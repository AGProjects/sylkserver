"""
Applications are great.  Lets have more of them.
"""
import weakref

from functools import wraps
from twisted.python.components import registerAdapter
from twisted.web.server import Request
from werkzeug.routing import Map, Rule
from zope.interface import implements

from sylk.web.klein.resource import KleinResource
from sylk.web.klein.interfaces import IKleinRequest

__all__ = ['Klein']


def _call(instance, f, *args, **kwargs):
    if instance is None:
        return f(*args, **kwargs)

    return f(instance, *args, **kwargs)


class KleinRequest(object):
    implements(IKleinRequest)

    def __init__(self, request):
        self.branch_segments = ['']
        self.mapper = None

    def url_for(self, *args, **kwargs):
        return self.mapper.build(*args, **kwargs)


registerAdapter(KleinRequest, Request, IKleinRequest)


class Klein(object):
    """
    L{Klein} is an object which is responsible for maintaining the routing
    configuration of our application.

    @ivar _url_map: A C{werkzeug.routing.Map} object which will be used for
        routing resolution.
    @ivar _endpoints: A C{dict} mapping endpoint names to handler functions.
    """

    _bound_klein_instances = weakref.WeakKeyDictionary()

    def __init__(self):
        self._url_map = Map()
        self._endpoints = {}
        self._error_handlers = []
        self._instance = None


    def __eq__(self, other):
        if isinstance(other, Klein):
            return vars(self) == vars(other)
        return NotImplemented


    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


    @property
    def url_map(self):
        """
        Read only property exposing L{Klein._url_map}.
        """
        return self._url_map


    @property
    def endpoints(self):
        """
        Read only property exposing L{Klein._endpoints}.
        """
        return self._endpoints


    def execute_endpoint(self, endpoint, *args, **kwargs):
        """
        Execute the named endpoint with all arguments and possibly a bound
        instance.
        """
        endpoint_f = self._endpoints[endpoint]
        return endpoint_f(self._instance, *args, **kwargs)


    def execute_error_handler(self, handler, request, failure):
        """
        Execute the passed error handler, possibly with a bound instance.
        """
        return handler(self._instance, request, failure)


    def resource(self):
        """
        Return an L{IResource} which suitably wraps this app.

        @returns: An L{IResource}
        """

        return KleinResource(self)


    def __get__(self, instance, owner):
        """
        Get an instance of L{Klein} bound to C{instance}.
        """
        if instance is None:
            return self

        k = self._bound_klein_instances.get(instance)

        if k is None:
            k = self.__class__()
            k._url_map = self._url_map
            k._endpoints = self._endpoints
            k._error_handlers = self._error_handlers
            k._instance = instance
            self._bound_klein_instances[instance] = k

        return k


    def route(self, url, *args, **kwargs):
        """
        Add a new handler for C{url} passing C{args} and C{kwargs} directly to
        C{werkzeug.routing.Rule}.  The handler function will be passed at least
        one argument an L{twisted.web.server.Request} and any keyword arguments
        taken from the C{url} pattern.

        ::
            @app.route("/")
            def index(request):
                return "Hello"

        @param url: A werkzeug URL pattern given to C{werkzeug.routing.Rule}.
        @type url: str

        @param branch: A bool indiciated if a branch endpoint should
            be added that allows all child path segments that don't
            match some other route to be consumed.  Default C{False}.
        @type branch: bool


        @returns: decorated handler function.
        """
        segment_count = url.count('/')
        if url.endswith('/'):
            segment_count -= 1

        def deco(f):
            kwargs.setdefault('endpoint', f.__name__)
            if kwargs.pop('branch', False):
                branchKwargs = kwargs.copy()
                branchKwargs['endpoint'] = branchKwargs['endpoint'] + '_branch'

                @wraps(f)
                def branch_f(instance, request, *a, **kw):
                    IKleinRequest(request).branch_segments = kw.pop('__rest__', '').split('/')
                    return _call(instance, f, request, *a, **kw)

                branch_f.segment_count = segment_count

                self._endpoints[branchKwargs['endpoint']] = branch_f
                self._url_map.add(Rule(url.rstrip('/') + '/' + '<path:__rest__>', *args, **branchKwargs))

            @wraps(f)
            def _f(instance, request, *a, **kw):
                return _call(instance, f, request, *a, **kw)

            _f.segment_count = segment_count

            self._endpoints[kwargs['endpoint']] = _f
            self._url_map.add(Rule(url, *args, **kwargs))
            return f

        return deco


    def handle_errors(self, f_or_exception, *additional_exceptions):
        """
        Register an error handler. This decorator supports two syntaxes. The
        simpler of these can be used to register a handler for all C{Exception}
        types::

            @app.handle_errors
            def error_handler(request, failure):
                request.setResponseCode(500)
                return 'Uh oh'

        Alternately, a handler can be registered for one or more specific
        C{Exception} tyes::

            @app.handle_errors(EncodingError, ValidationError):
            def error_handler(request, failure)
                request.setResponseCode(400)
                return failure.getTraceback()

        The handler will be passed a L{twisted.web.server.Request} as well as a
        L{twisted.python.failure.Failure} instance. Error handlers may return a
        deferred, a failure or a response body.

        If more than one error handler is registered, the handlers will be
        executed in the order in which they are defined, until a handler is
        encountered which completes successfully. If no handler completes
        successfully, L{twisted.web.server.Request}'s processingFailed() method
        will be called.

        In addition to handling errors that occur within a route handler, error
        handlers also handle any C{werkzeug.exceptions.HTTPException} which is
        raised during routing. In particular, C{werkzeug.exceptions.NotFound}
        will be raised if no matching route is found, so to return a custom 404
        users can do the following::

            @app.handle_errors(NotFound)
            def error_handler(request, failure):
                request.setResponseCode(404)
                return 'Not found'

        @param f_or_exception: An error handler function, or an C{Exception}
            subclass to scope the decorated handler to.
        @type f_or_exception: C{function} or C{Exception}

        @param additional_exceptions Additional C{Exception} subclasses to
            scope the decorated function to.
        @type additional_exceptions C{list} of C{Exception}s

        @returns: decorated error handler function.
        """
        # Try to detect calls using the "simple" @app.handle_error syntax by
        # introspecting the first argument - if it isn't a type which
        # subclasses Exception we assume the simple syntax was used.
        if not isinstance(f_or_exception, type) or not issubclass(f_or_exception, Exception):
            return self.handle_errors(Exception)(f_or_exception)

        def deco(f):
            @wraps(f)
            def _f(instance, request, failure):
                return _call(instance, f, request, failure)

            self._error_handlers.append(([f_or_exception] + list(additional_exceptions), _f))
            return _f

        return deco

