"""Views"""
from django.conf import settings
from django.http import HttpResponseRedirect, HttpResponse, \
                        HttpResponseServerError
from django.core.urlresolvers import reverse
from django.db import transaction
from django.contrib.auth import login, REDIRECT_FIELD_NAME
from django.contrib.auth.decorators import login_required

from social_auth.backends import get_backend


def auth(request, backend):
    """Start authentication process"""
    complete_url = getattr(settings, 'SOCIAL_AUTH_COMPLETE_URL_NAME',
                           'complete')
    redirect = getattr(settings, 'LOGIN_REDIRECT_URL', '')
    return auth_process(request, backend, complete_url, redirect)


@transaction.commit_on_success
def complete(request, backend):
    """Authentication complete view, override this view if transaction
    management doesn't suit your needs."""
    return complete_process(request, backend)


def complete_process(request, backend):
    """Authentication complete process"""
    backend = get_backend(backend, request, request.path)
    if not backend:
        return HttpResponseServerError('Incorrect authentication service')

    try:
        user = backend.auth_complete()
    except ValueError, e: # some Authentication error ocurred
        user = None
        error_key = getattr(settings, 'SOCIAL_AUTH_ERROR_KEY', None)
        if error_key: # store error in session
            request.session[error_key] = str(e)

    if user and getattr(user, 'is_active', True):
        login(request, user)
        # set session expiration date if present
        social_user = user.social_auth.get(provider=backend.AUTH_BACKEND.name)
        if social_user.expiration_delta():
            request.session.set_expiry(social_user.expiration_delta())
        url = request.session.pop(REDIRECT_FIELD_NAME, '') or \
              getattr(settings, 'LOGIN_REDIRECT_URL', '')
    else:
        url = getattr(settings, 'LOGIN_ERROR_URL', settings.LOGIN_URL)
    return HttpResponseRedirect(url)


@login_required
def associate(request, backend):
    """Authentication starting process"""
    complete_url = getattr(settings, 'SOCIAL_AUTH_ASSOCIATE_URL_NAME',
                           'associate_complete')
    redirect = getattr(settings, 'LOGIN_REDIRECT_URL', '')
    return auth_process(request, backend, complete_url, redirect)


@login_required
def associate_complete(request, backend):
    """Authentication complete process"""
    backend = get_backend(backend, request, request.path)
    if not backend:
        return HttpResponseServerError('Incorrect authentication service')
    backend.auth_complete(user=request.user)
    url = request.session.pop(REDIRECT_FIELD_NAME, '') or \
          getattr(settings, 'LOGIN_REDIRECT_URL', '')
    return HttpResponseRedirect(url)


@login_required
def disconnect(request, backend):
    """Disconnects given backend from current logged in user."""
    backend = get_backend(backend, request, request.path)
    if not backend:
        return HttpResponseServerError('Incorrect authentication service')
    backend.disconnect(request.user)
    url = request.REQUEST.get(REDIRECT_FIELD_NAME, '') or \
          getattr(settings, 'LOGIN_REDIRECT_URL', '')
    return HttpResponseRedirect(url)


def auth_process(request, backend, complete_url_name, default_final_url):
    """Authenticate using social backend"""
    redirect = reverse(complete_url_name, args=(backend,))
    backend = get_backend(backend, request, redirect)
    if not backend:
        return HttpResponseServerError('Incorrect authentication service')
    data = request.REQUEST
    request.session[REDIRECT_FIELD_NAME] = data.get(REDIRECT_FIELD_NAME,
                                                    default_final_url)
    if backend.uses_redirect:
        return HttpResponseRedirect(backend.auth_url())
    else:
        return HttpResponse(backend.auth_html(),
                            content_type='text/html;charset=UTF-8')
