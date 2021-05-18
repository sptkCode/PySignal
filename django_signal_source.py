import threading
import weakref

from django.utils.inspect import func_accepts_kwargs


def _make_id(target):
    """
    生成ID
    :param target:
    :return:
    """
    if hasattr(target, '__func__'):
        # 注意如果是一个对象方法，则使用对象以及方法的组合作为ID
        return id(target.__self__), id(target.__func__)
    # 如果其他则直接返回对象的ID值作为身份ID
    return id(target)


# 空ID值
NONE_ID = _make_id(None)

# A marker for caching
# 空对象表示没有接收者
NO_RECEIVERS = object()


class Signal:
    """
    Base class for all signals

    Internal attributes:

        receivers
            { receiverkey (id) : weakref(receiver) }
    """

    def __init__(self, providing_args=None, use_caching=False):
        """
        Create a new signal.

        providing_args
            A list of the arguments this signal can pass along in a send() call.
        """
        # 接收者，是可以调用的对象，例如，函数、方法、可以调用对象__call__
        self.receivers = []
        # 信号可提供的参数,一般我们用于创建信号的时候创建
        if providing_args is None:
            providing_args = []

        # 去重处理保证提可提供的参数唯一
        self.providing_args = set(providing_args)
        # 线程锁，保证添加或者删除修改信号方法的锁
        self.lock = threading.Lock()
        # 是否使用缓存，缺省不使用
        self.use_caching = use_caching
        # For convenience we create empty caches even if they are not used.
        # A note about caching: if use_caching is defined, then for each
        # distinct sender we cache the receivers that sender has in
        # 'sender_receivers_cache'. The cache is cleaned when .connect() or
        # .disconnect() is called and populated on send().
        # 创建缓存，WeakKeyDictionary用于创建告诉缓存，防止缓存存在类似相互引用而导致缓存不能删除问题
        self.sender_receivers_cache = weakref.WeakKeyDictionary() if use_caching else {}
        # 设置是否有以及消亡的接收者，例如某个线程结束后，那么这个接收者也就消失了.
        self._dead_receivers = False

    def connect(self, receiver, sender=None, weak=True, dispatch_uid=None):
        """
        Connect receiver to sender for signal.

        Arguments:

            receiver
                A function or an instance method which is to receive signals.
                Receivers must be hashable objects.

                If weak is True, then receiver must be weak referenceable.

                Receivers must be able to accept keyword arguments.

                If a receiver is connected with a dispatch_uid argument, it
                will not be added if another receiver was already connected
                with that dispatch_uid.

            sender
                The sender to which the receiver should respond. Must either be
                a Python object, or None to receive events from any sender.

            weak
                Whether to use weak references to the receiver. By default, the
                module will attempt to use weak references to the receiver
                objects. If this parameter is false, then strong references will
                be used.

            dispatch_uid
                An identifier used to uniquely identify a particular instance of
                a receiver. This will usually be a string, though it may be
                anything hashable.
        """
        # 加载django 配置
        from django.conf import settings

        # If DEBUG is on, check that we got a good receiver
        # 检查接收者的合法性，必须是可被调用的。
        if settings.configured and settings.DEBUG:
            #  断言接收者必须是一个可调用对象
            assert callable(receiver), "Signal receivers must be callable."

            # Check for **kwargs
            # 接收者必须**kwargs定义来接受参数，要求我们定义星号处理方法必须是**kwargs
            if not func_accepts_kwargs(receiver):
                raise ValueError("Signal receivers must accept keyword arguments (**kwargs).")

        # dispatch_uid 接收者身份ID，唯一的，通常是一个str类型
        if dispatch_uid:
            lookup_key = (dispatch_uid, _make_id(sender))
        else:
            # 注意查询key使用了receiver+sender的组合id作为key值
            lookup_key = (_make_id(receiver), _make_id(sender))

        # weak默认值True，是否使用弱引用[另一个主题]，主要解决缓存对象回收问题
        if weak:
            ref = weakref.ref
            receiver_object = receiver
            # Check for bound methods，检查是对象方法
            if hasattr(receiver, '__self__') and hasattr(receiver, '__func__'):
                ref = weakref.WeakMethod
                receiver_object = receiver.__self__

            # 创建若引用receiver
            receiver = ref(receiver)
            # 注册一个回调处理方法，当receiver_object被垃圾回收期回收的时候，调用
            # _remove_receiver 标识下有receiver已经死亡，可以移除了。 主要是在多线程场景下面可能发生
            weakref.finalize(receiver_object, self._remove_receiver)

        # 加锁，添加receiver，到我们的列表中
        with self.lock:
            # 清理已经死掉的receiver
            self._clear_dead_receivers()
            # 如果不存在则添加到列表
            if not any(r_key == lookup_key for r_key, _ in self.receivers):
                self.receivers.append((lookup_key, receiver))
            # 清理缓存，因为我们新增内容了
            self.sender_receivers_cache.clear()

    # 移除接收者从我们的信号中
    def disconnect(self, receiver=None, sender=None, dispatch_uid=None):
        """
        Disconnect receiver from sender for signal.

        If weak references are used, disconnect need not be called. The receiver
        will be removed from dispatch automatically.

        Arguments:

            receiver
                The registered receiver to disconnect. May be none if
                dispatch_uid is specified.

            sender
                The registered sender to disconnect

            dispatch_uid
                the unique identifier of the receiver to disconnect
        """
        # 生成lookup_key
        if dispatch_uid:
            lookup_key = (dispatch_uid, _make_id(sender))
        else:
            lookup_key = (_make_id(receiver), _make_id(sender))

        # 设置默认值False
        disconnected = False
        # 加锁
        with self.lock:
            # 清理死掉的receiver，我们可以提取清理一次死去的
            self._clear_dead_receivers()
            # 遍历判断如果是我们要删除的receiver则删除
            for index in range(len(self.receivers)):
                (r_key, _) = self.receivers[index]
                if r_key == lookup_key:
                    disconnected = True
                    del self.receivers[index]
                    break
            # 清理缓存，保证我们缓存使用的是最新的
            self.sender_receivers_cache.clear()
        # 返回disconnected 标志
        return disconnected

    def has_listeners(self, sender=None):
        return bool(self._live_receivers(sender))

    # 发送信号---->通知所有的receiver，任何一个receiver出现错误则会抛出异常，并终止
    def send(self, sender, **named):
        """
        Send signal from sender to all connected receivers.

        If any receiver raises an error, the error propagates back through send,
        terminating the dispatch loop. So it's possible that all receivers
        won't be called if an error is raised.

        Arguments:

            sender
                The sender of the signal. Either a specific object or None.

            named
                Named arguments which will be passed to receivers.

        Return a list of tuple pairs [(receiver, response), ... ].
        """
        # 如果没有receivers 或者 没有找到对应的sender 则直接返回空
        if not self.receivers or self.sender_receivers_cache.get(sender) is NO_RECEIVERS:
            return []

        # 循环调用执行receiver
        return [
            (receiver, receiver(signal=self, sender=sender, **named))
            for receiver in self._live_receivers(sender)
        ]

    # 与send的区别在于，这里面处理响应的异常，保证其他的接收则可以正常执行
    def send_robust(self, sender, **named):
        """
        Send signal from sender to all connected receivers catching errors.

        Arguments:

            sender
                The sender of the signal. Can be any Python object (normally one
                registered with a connect if you actually want something to
                occur).

            named
                Named arguments which will be passed to receivers. These
                arguments must be a subset of the argument names defined in
                providing_args.

        Return a list of tuple pairs [(receiver, response), ... ].

        If any receiver raises an error (specifically any subclass of
        Exception), return the error instance as the result for that receiver.
        """
        if not self.receivers or self.sender_receivers_cache.get(sender) is NO_RECEIVERS:
            return []

        # Call each receiver with whatever arguments it can accept.
        # Return a list of tuple pairs [(receiver, response), ... ].
        responses = []
        for receiver in self._live_receivers(sender):
            try:
                # 尝试执行接收者任务
                response = receiver(signal=self, sender=sender, **named)
            except Exception as err:
                # 如果错误则把错误信息添加到响应中
                responses.append((receiver, err))
            else:
                responses.append((receiver, response))
        # 返回信号处理的结果
        return responses

    # 清理死掉的接收则
    def _clear_dead_receivers(self):
        # Note: caller is assumed to hold self.lock.
        if self._dead_receivers:
            # 重新设置标志,为False
            self._dead_receivers = False
            # 保留存活的 receivers，而不是删除死去的
            self.receivers = [
                r for r in self.receivers
                # 如果不是引用类型 以及 已经销毁则说明死亡，取反后则说明剩下就是存活的。
                if not (isinstance(r[1], weakref.ReferenceType) and r[1]() is None)
            ]

    # 过滤获取存活的receiver
    def _live_receivers(self, sender):
        """
        Filter sequence of receivers to get resolved, live receivers.

        This checks for weak references and resolves them, then returning only
        live receivers.
        """
        receivers = None
        # 如果有缓存，且没有死亡的接收则从缓存获取数据
        if self.use_caching and not self._dead_receivers:
            receivers = self.sender_receivers_cache.get(sender)
            # We could end up here with NO_RECEIVERS even if we do check this case in
            # .send() prior to calling _live_receivers() due to concurrent .send() call.
            if receivers is NO_RECEIVERS:
                return []

        # 如果缓存中没有获取到
        if receivers is None:
            # 加锁
            with self.lock:
                # 清理死掉的receiver
                self._clear_dead_receivers()
                senderkey = _make_id(sender)
                receivers = []
                # 遍历获取存活的receiver
                for (receiverkey, r_senderkey), receiver in self.receivers:
                    if r_senderkey == NONE_ID or r_senderkey == senderkey:
                        receivers.append(receiver)
                # 如果设置缓存，则把存活的receiver添加到缓存
                if self.use_caching:
                    if not receivers:
                        self.sender_receivers_cache[sender] = NO_RECEIVERS
                    else:
                        # Note, we must cache the weakref versions.
                        self.sender_receivers_cache[sender] = receivers

        # 创建一个空列表用于保存正常的receiver便于调用使用
        non_weak_receivers = []
        for receiver in receivers:
            if isinstance(receiver, weakref.ReferenceType):
                # Dereference the weak reference.
                # 解弱引用，转化成常规对象
                receiver = receiver()
                if receiver is not None:
                    # 如果返回不是None则添加到列表中
                    non_weak_receivers.append(receiver)
            else:
                non_weak_receivers.append(receiver)
        # 返回常规的receivers 便于我们下一步继续使用
        return non_weak_receivers

    def _remove_receiver(self, receiver=None):
        # Mark that the self.receivers list has dead weakrefs. If so, we will
        # clean those up in connect, disconnect and _live_receivers while
        # holding self.lock. Note that doing the cleanup here isn't a good
        # idea, _remove_receiver() will be called as side effect of garbage
        # collection, and so the call can happen while we are already holding
        # self.lock.
        # 设置标志，说明有receiver死掉了
        self._dead_receivers = True



# 信号装饰器,用于连接信号
def receiver(signal, **kwargs):
    """
    A decorator for connecting receivers to signals. Used by passing in the
    signal (or list of signals) and keyword arguments to connect::

        @receiver(post_save, sender=MyModel)
        def signal_receiver(sender, **kwargs):
            ...

        @receiver([post_save, post_delete], sender=MyModel)
        def signals_receiver(sender, **kwargs):
            ...
    """

    def _decorator(func):
        if isinstance(signal, (list, tuple)):
            for s in signal:
                s.connect(func, **kwargs)
        else:
            signal.connect(func, **kwargs)
        return func

    return _decorator
