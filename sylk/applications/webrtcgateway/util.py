
from eventlib.coros import event


class IdentityFormatter(object):
    @classmethod
    def format(cls, identity):
        if identity.display_name:
            return u'%s <sip:%s@%s>' % (identity.display_name, identity.uri.user, identity.uri.host)
        else:
            return u'sip:%s@%s' % (identity.uri.user, identity.uri.host)


class GreenEvent(object):
    def __init__(self):
        self._event = event()

    def set(self):
        if self._event.ready():
            return
        self._event.send(True)

    def is_set(self):
        return self._event.ready()

    def clear(self):
        if self._event.ready():
            self._event.reset()

    def wait(self):
        return self._event.wait()

