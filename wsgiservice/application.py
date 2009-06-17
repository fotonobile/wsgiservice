"""Components responsible for building the WSGI application."""
import hashlib
import inspect
import logging
import re
import webob
import wsgiservice
from wsgiservice import Response
from wsgiservice.objects import MiniResponse
from wsgiservice.exceptions import ValidationException

logger = logging.getLogger(__name__)

class Application(object):
    """WSGI application wrapping a set of WsgiService resources. This class
    can be used as a WSGI application according to :pep:`333`.

    :param resources: A list of :class:`wsgiservice.Resource` classes to be
                      served by this application.

    .. todo:: Think about how to handle 201, 200/204 methods.
    .. todo:: Make downtime configurable with a file or something like that?
       Could then send out a 503 response with proper Retry-After header.
    .. todo:: Allow easy pluggin in of a compression WSGI middleware
    .. todo:: Convert to requested charset with Accept-Charset header
    .. todo:: Return Allow header as response to PUT and for 405 (also 501?)
    .. todo:: Implement Content-Location header
    .. todo:: Log From and Referer headers
    .. todo:: On 201 created provide Location header
    .. todo:: Abstract away error and status code handling
    .. todo:: Easy deployment using good configuration file handling
    .. todo:: Create usable REST API documentation from source
    .. todo:: Support OPTIONS, send out the Allow header, and some
       machine-readable output in the correct format. ``OPTIONS *`` can be
       discarded as NOOP
    .. todo:: Must return different ETags for different representations of a
       resource.
    """
    def __init__(self, resources):
        self._resources = resources
        self._urlmap = wsgiservice.routing.Router(resources)

    def __call__(self, environ, start_response):
        """WSGI entry point. Serve the best matching resource for the current
        request.
        """
        # Find the correct resource
        res = self._handle_request(environ)
        if isinstance(res, Response):
            b = str(res)
            start_response(res.status, res.headers)
            return b

    def _handle_request(self, environ):
        path = environ['PATH_INFO']
        parsed = self._urlmap(path)
        if not parsed:
            return self._get_response_404(None, environ)
        else:
            path_params, res = parsed
            return self._call_resource(res, path_params, environ)

    def _call_resource(self, res, path_params, environ):
        request = webob.Request(environ)
        instance = res()
        method = self._resolve_method(instance, request.method)
        if not method:
            if request.method in ('OPTIONS', 'GET', 'HEAD', 'POST', 'PUT',
                                  'DELETE', 'TRACE', 'CONNECT'):
                return self._get_response_405(instance, environ)
            else:
                return self._get_response_501(instance, environ)
        default_headers, response = self._handle_conditions(instance,
            path_params, environ, request)
        if response:
            return response
        body, headers = self._call_dynamic_method(instance, method,
            path_params, request), None
        if isinstance(body, MiniResponse):
            body, headers = body.body, body.headers
        if not headers:
            headers = {}
        for key, value in default_headers.iteritems():
            if value and key not in headers:
                headers[key] = value
        return Response(body, environ, instance, method,
            headers =headers,
            extension=path_params.get('_extension', None))

    def _resolve_method(self, instance, method):
        if hasattr(instance, method) and callable(getattr(instance, method)):
            return method
        elif method == 'HEAD':
            return self._resolve_method(instance, 'GET')
        return None

    def _handle_conditions(self, instance, path_params, environ, request):
        if 'HTTP_CONTENT_MD5' in environ:
            environ['wsgi.input'].seek(0)
            body_md5 = hashlib.md5(environ['wsgi.input'].read()).hexdigest()
            if body_md5 != environ['HTTP_CONTENT_MD5']:
                return {}, self._get_response_400(instance, environ,
                    body='The Content-MD5 request header does not match the body.')
        etag = self._get_etag(instance, path_params, request)
        last_modified = self._get_last_modified(instance, path_params, request)
        headers = {'ETag': etag,
                   'Last-Modified': webob._serialize_date(last_modified)}
        status = self._handle_condition_etag(request, etag)
        if status == 0:
            status = self._handle_condition_last_modified(request, last_modified)
        if status > 0:
            resfunc = getattr(self, '_get_response_' + str(status))
            return headers, resfunc(instance, environ, headers)
        return headers, None

    def _handle_condition_etag(self, request, etag):
        if not etag:
            return 0
        etag_match = etag.replace('"', '')
        if not etag_match in request.if_match:
            return 412
        if etag_match in request.if_none_match:
            if request.method in ('GET', 'HEAD'):
                return 304
            else:
                return 412
        return 0

    def _handle_condition_last_modified(self, request, last_modified):
        if not last_modified:
            return
        if request.if_modified_since and last_modified <= request.if_modified_since:
            return 304
        if request.if_unmodified_since and last_modified > request.if_unmodified_since:
            return 412
        return 0

    def _get_response_304(self, instance, environ, headers):
        logger.info("HTTP statuc 304: Not modified")
        return Response(None, environ, instance, status=304, headers=headers)

    def _get_response_400(self, instance, environ, headers={}, body='Invalid request'):
        logger.error("HTTP error 400: %s", body)
        return Response({'error': body}, environ, instance, status=400,
            headers=headers)

    def _get_response_404(self, instance, environ, headers={}):
        logger.error("HTTP error 404: Not found")
        return Response({'error': 'not found'}, environ, instance, status=404,
            headers=headers)

    def _get_response_405(self, instance, environ, headers={}):
        logger.error("HTTP error 405: Invalid method on resource")
        headers['Allow'] = self._get_allowed_methods(instance)
        return Response({'error': 'Invalid method on resource'}, environ,
            instance, status=405, headers=headers)

    def _get_response_412(self, instance, environ, headers):
        logger.error("HTTP error 412: Precondition failed")
        return Response({'error': 'Precondition failed'}, environ,
            instance, status=412, headers=headers)

    def _get_response_501(self, instance, environ, headers={}):
        logger.error("HTTP error 501: Unknown method")
        headers['Allow'] = self._get_allowed_methods(instance)
        return Response({'error': 'Unknown method'}, environ,
            instance, status=501, headers=headers)

    def _get_etag(self, instance, path_params, request):
        if not hasattr(instance, 'get_etag'):
            return None
        retval = self._call_dynamic_method(instance, 'get_etag', path_params,
            request)
        if retval:
            return '"' + retval.replace('"', '') + '"'

    def _get_last_modified(self, instance, path_params, request):
        if not hasattr(instance, 'get_last_modified'):
            return None
        return self._call_dynamic_method(instance, 'get_last_modified',
            path_params, request)

    def _get_allowed_methods(self, instance):
        return ", ".join([method for method in dir(instance)
            if method.upper() == method
            and callable(getattr(instance, method))])

    def _call_dynamic_method(self, instance, method, path_params, request):
        method = getattr(instance, method)
        method_params, varargs, varkw, defaults = inspect.getargspec(method)
        if method_params:
            method_params.pop(0) # pop the self off
        params = []
        for param in method_params:
            value = None
            if param == 'request':
                value = request
            elif param in path_params:
                value = path_params[param]
            elif param in request.GET:
                value = request.GET[param]
            elif param in request.POST:
                value = request.POST[param]
            self._validate_param(method, param, value)
            params.append(value)
        return method(*params)

    def _validate_param(self, method, param, value):
        rules = None
        if hasattr(method, '_validations') and param in method._validations:
            rules = method._validations[param]
        elif hasattr(method.im_class, '_validations') and param in method.im_class._validations:
            rules = method.im_class._validations[param]
        if rules is None:
            return
        if value is None or len(value) == 0:
            raise ValidationException("Value for {0} must not be empty.".format(param))
        elif 're' in rules and rules['re']:
            if not re.search('^' + rules['re'] + '$', value):
                raise ValidationException("{0} value {1} does not validate.".format(param, value))

def get_app(defs):
    """Small wrapper function to returns an instance of :class:`Application`
    which serves the objects in the defs. Usually this is called with return
    value globals() from the module where the resources are defined. The
    returned WSGI application will serve all subclasses of
    :class:`wsgiservice.Resource` found in the dictionary.

    :param defs: Each :class:`wsgiservice.Resource` object found in the values
                 of this dictionary is used as application resource. The other
                 values are discarded.
    :type defs: dict
    :rtype: :class:`Application`
    """
    if isinstance(defs, tuple):
        # A list of different applications mounted at different paths
        # TODO
        defs = defs[1]
    resources = [d for d in defs.values() if d in wsgiservice.Resource.__subclasses__()]
    return Application(resources)
