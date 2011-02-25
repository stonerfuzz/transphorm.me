"""
Base backends structures.

This module defines base classes needed to define custom OpenID or OAuth
auth services from third parties. This customs must subclass an Auth and
and Backend class, check current implementation for examples.

Also the modules *must* define a BACKENDS dictionary with the backend name
(which is used for URLs matching) and Auth class, otherwise it won't be
enabled.
"""
from os import urandom, walk
from os.path import basename
from httplib import HTTPSConnection

from openid.consumer.consumer import Consumer, SUCCESS, CANCEL, FAILURE
from openid.consumer.discover import DiscoveryFailure
from openid.extensions import sreg, ax

from oauth.oauth import OAuthConsumer, OAuthToken, OAuthRequest, \
                        OAuthSignatureMethod_HMAC_SHA1

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.backends import ModelBackend
from django.utils.hashcompat import md5_constructor
from django.utils.importlib import import_module

from social_auth.models import UserSocialAuth
from social_auth.store import DjangoOpenIDStore
from social_auth.signals import pre_update, socialauth_registered


# key for username in user details dict used around, see get_user_details
# method
USERNAME = 'username'

# OpenID configuration
OLD_AX_ATTRS = [
    ('http://schema.openid.net/contact/email', 'old_email'),
    ('http://schema.openid.net/namePerson', 'old_fullname'),
    ('http://schema.openid.net/namePerson/friendly', 'old_nickname')
]
AX_SCHEMA_ATTRS = [
    # Request both the full name and first/last components since some
    # providers offer one but not the other.
    ('http://axschema.org/contact/email', 'email'),
    ('http://axschema.org/namePerson', 'fullname'),
    ('http://axschema.org/namePerson/first', 'first_name'),
    ('http://axschema.org/namePerson/last', 'last_name'),
    ('http://axschema.org/namePerson/friendly', 'nickname'),
]
SREG_ATTR = [
    ('email', 'email'),
    ('fullname', 'fullname'),
    ('nickname', 'nickname')
]
OPENID_ID_FIELD = 'openid_identifier'
SESSION_NAME = 'openid'

# get User class, could not be auth.User
User = UserSocialAuth._meta.get_field('user').rel.to


class SocialAuthBackend(ModelBackend):
    """A django.contrib.auth backend that authenticates the user based on
    a authentication provider response"""
    name = ''  # provider name, it's stored in database

    def authenticate(self, *args, **kwargs):
        """Authenticate user using social credentials

        Authentication is made if this is the correct backend, backend
        verification is made by kwargs inspection for current backend
        name presence.
        """
        # Validate backend and arguments. Require that the Social Auth
        # response be passed in as a keyword argument, to make sure we
        # don't match the username/password calling conventions of
        # authenticate.
        if not (self.name and kwargs.get(self.name) and 'response' in kwargs):
            return None

        response = kwargs.get('response')
        details = self.get_user_details(response)
        uid = self.get_user_id(details, response)
        is_new = False
        try:
            social_user = UserSocialAuth.objects.select_related('user')\
                                                .get(provider=self.name,
                                                     uid=uid)
        except UserSocialAuth.DoesNotExist:
            user = kwargs.get('user')
            if user is None:  # new user
                if not getattr(settings, 'SOCIAL_AUTH_CREATE_USERS', True):
                    return None
                username = self.username(details)
                email = details.get('email')
                user = User.objects.create_user(username=username, email=email)
                is_new = True
            social_user = self.associate_auth(user, uid, response, details)
        else:
            # This account was registered to another user, so we raise an
            # error in such case and the view should decide what to do on
            # at this moment, merging account is not an option because that
            # would imply update user references on other apps, that's too
            # much intrusive
            if 'user' in kwargs and kwargs['user'] != social_user.user:
                raise ValueError('Account already in use.')
            user = social_user.user

        # Update user account data.
        self.update_user_details(user, response, details, is_new)

        # Update extra_data storage, unless disabled by setting
        if getattr(settings, 'SOCIAL_AUTH_EXTRA_DATA', True):
            extra_data = self.extra_data(user, uid, response, details)
            if extra_data and social_user.extra_data != extra_data:
                social_user.extra_data = extra_data
                social_user.save()

        return user

    def username(self, details):
        """Return an unique username, if SOCIAL_AUTH_FORCE_RANDOM_USERNAME
        setting is True, then username will be a random 30 chars md5 hash
        """
        def get_random_username():
            """Return hash from random string cut at 30 chars"""
            return md5_constructor(urandom(10)).hexdigest()[:30]

        if getattr(settings, 'SOCIAL_AUTH_FORCE_RANDOM_USERNAME', False):
            username = get_random_username()
        elif USERNAME in details:
            username = details[USERNAME]
        elif hasattr(settings, 'SOCIAL_AUTH_DEFAULT_USERNAME'):
            username = settings.SOCIAL_AUTH_DEFAULT_USERNAME
            if callable(username):
                username = username()
        else:
            username = get_random_username()

        fixer = getattr(settings, 'SOCIAL_AUTH_USERNAME_FIXER', lambda u: u)

        name, idx = username, 2
        while True:
            try:
                name = fixer(name)
                User.objects.get(username=name)
                name = username + str(idx)
                idx += 1
            except User.DoesNotExist:
                username = name
                break
        return username

    def associate_auth(self, user, uid, response, details):
        """Associate a Social Auth with an user account."""
        return UserSocialAuth.objects.create(user=user, uid=uid,
                                             provider=self.name)

    def extra_data(self, user, uid, response, details):
        """Return default blank user extra data"""
        return ''

    def update_user_details(self, user, response, details, is_new=False):
        """Update user details with (maybe) new data. Username is not
        changed if associating a new credential."""
        changed = False  # flag to track changes

        # check if values update should be left to signals handlers only
        if not getattr(settings, 'SOCIAL_AUTH_CHANGE_SIGNAL_ONLY', False):
            for name, value in details.iteritems():
                # do not update username, it was already generated by
                # self.username(...) and loaded in given instance
                if name != USERNAME and value and value != getattr(user, name,
                                                                   value):
                    setattr(user, name, value)
                    changed = True

        # Fire a pre-update signal sending current backend instance,
        # user instance (created or retrieved from database), service
        # response and processed details.
        #
        # Also fire socialauth_registered signal for newly registered
        # users.
        #
        # Signal handlers must return True or False to signal instance
        # changes. Send method returns a list of tuples with receiver
        # and it's response.
        signal_response = lambda (receiver, response): response

        kwargs = {'sender': self.__class__, 'user': user,
                  'response': response, 'details': details}
        changed |= any(filter(signal_response, pre_update.send(**kwargs)))

        # Fire socialauth_registered signal on new user registration
        if is_new:
            changed |= any(filter(signal_response,
                                  socialauth_registered.send(**kwargs)))

        if changed:
            user.save()

    def get_user_id(self, details, response):
        """Must return a unique ID from values returned on details"""
        raise NotImplementedError('Implement in subclass')

    def get_user_details(self, response):
        """Must return user details in a know internal struct:
            {USERNAME: <username if any>,
             'email': <user email if any>,
             'fullname': <user full name if any>,
             'first_name': <user first name if any>,
             'last_name': <user last name if any>}
        """
        raise NotImplementedError('Implement in subclass')

    def get_user(self, user_id):
        """Return user instance for @user_id"""
        try:
            return User.objects.get(pk=user_id)
        except User.DoesNotExist:
            return None


class OAuthBackend(SocialAuthBackend):
    """OAuth authentication backend base class.

    EXTRA_DATA defines a set of name that will be stored in
               extra_data field. It must be a list of tuples with
               name and alias.

    Also settings will be inspected to get more values names that should be
    stored on extra_data field. Setting name is created from current backend
    name (all uppercase) plus _EXTRA_DATA.

    access_token is always stored.
    """
    EXTRA_DATA = None

    def get_user_id(self, details, response):
        "OAuth providers return an unique user id in response"""
        return response['id']

    def extra_data(self, user, uid, response, details):
        """Return access_token and extra defined names to store in
        extra_data field"""
        data = {'access_token': response.get('access_token', '')}
        name = self.name.replace('-', '_').upper()
        names = self.EXTRA_DATA or [] + \
                getattr(settings, name + '_EXTRA_DATA', [])
        data.update((alias, response.get(name)) for name, alias in names)
        return data


class OpenIDBackend(SocialAuthBackend):
    """Generic OpenID authentication backend"""
    name = 'openid'

    def get_user_id(self, details, response):
        """Return user unique id provided by service"""
        return response.identity_url

    def values_from_response(self, response, sreg_names=None, ax_names=None):
        """Return values from SimpleRegistration response or
        AttributeExchange response if present.

        @sreg_names and @ax_names must be a list of name and aliases
        for such name. The alias will be used as mapping key.
        """
        values = {}

        # Use Simple Registration attributes if provided
        if sreg_names:
            resp = sreg.SRegResponse.fromSuccessResponse(response)
            if resp:
                values.update((alias, resp.get(name) or '')
                                    for name, alias in sreg_names)

        # Use Attribute Exchange attributes if provided
        if ax_names:
            resp = ax.FetchResponse.fromSuccessResponse(response)
            if resp:
                values.update((alias.replace('old_', ''),
                               resp.getSingle(src, ''))
                                for src, alias in ax_names)
        return values

    def get_user_details(self, response):
        """Return user details from an OpenID request"""
        values = {USERNAME: '', 'email': '', 'fullname': '',
                  'first_name': '', 'last_name': ''}
        # update values using SimpleRegistration or AttributeExchange
        # values
        values.update(self.values_from_response(response,
                                                SREG_ATTR,
                                                OLD_AX_ATTRS + \
                                                AX_SCHEMA_ATTRS))

        fullname = values.get('fullname') or ''
        first_name = values.get('first_name') or ''
        last_name = values.get('last_name') or ''

        if not fullname and first_name and last_name:
            fullname = first_name + ' ' + last_name
        elif fullname:
            try:  # Try to split name for django user storage
                first_name, last_name = fullname.rsplit(' ', 1)
            except ValueError:
                last_name = fullname

        values.update({'fullname': fullname, 'first_name': first_name,
                       'last_name': last_name,
                       USERNAME: values.get(USERNAME) or \
                                   (first_name.title() + last_name.title())})
        return values

    def extra_data(self, user, uid, response, details):
        """Return defined extra data names to store in extra_data field.
        Settings will be inspected to get more values names that should be
        stored on extra_data field. Setting name is created from current
        backend name (all uppercase) plus _SREG_EXTRA_DATA and
        _AX_EXTRA_DATA because values can be returned by SimpleRegistration
        or AttributeExchange schemas.

        Both list must be a value name and an alias mapping similar to
        SREG_ATTR, OLD_AX_ATTRS or AX_SCHEMA_ATTRS
        """
        name = self.name.replace('-', '_').upper()
        sreg_names = getattr(settings, name + '_SREG_EXTRA_DATA', None)
        ax_names = getattr(settings, name + '_AX_EXTRA_DATA', None)
        data = self.values_from_response(response, ax_names, sreg_names)
        return data


class BaseAuth(object):
    """Base authentication class, new authenticators should subclass
    and implement needed methods.

        @AUTH_BACKEND   Authorization backend related with this service
    """
    AUTH_BACKEND = None

    def __init__(self, request, redirect):
        self.request = request
        self.data = request.POST if request.method == 'POST' else request.GET
        self.redirect = redirect

    def auth_url(self):
        """Must return redirect URL to auth provider"""
        raise NotImplementedError('Implement in subclass')

    def auth_html(self):
        """Must return login HTML content returned by provider"""
        raise NotImplementedError('Implement in subclass')

    def auth_complete(self, *args, **kwargs):
        """Completes loging process, must return user instance"""
        raise NotImplementedError('Implement in subclass')

    @property
    def uses_redirect(self):
        """Return True if this provider uses redirect url method,
        otherwise return false."""
        return True

    @classmethod
    def enabled(cls):
        """Return backend enabled status, all enabled by default"""
        return True

    def disconnect(self, user):
        """Deletes current backend from user if associated.
        Override if extra operations are needed.
        """
        user.social_auth.filter(provider=self.AUTH_BACKEND.name).delete()


class OpenIdAuth(BaseAuth):
    """OpenId process handling"""
    AUTH_BACKEND = OpenIDBackend

    def auth_url(self):
        openid_request = self.setup_request()
        # Construct completion URL, including page we should redirect to
        return_to = self.request.build_absolute_uri(self.redirect)
        trust_root = getattr(settings, 'OPENID_TRUST_ROOT',
                             self.request.build_absolute_uri('/'))
        return openid_request.redirectURL(trust_root, return_to)

    def auth_html(self):
        openid_request = self.setup_request()
        return_to = self.request.build_absolute_uri(self.redirect)
        trust_root = getattr(settings, 'OPENID_TRUST_ROOT',
                             self.request.build_absolute_uri('/'))
        form_tag = {'id': 'openid_message'}
        return openid_request.htmlMarkup(trust_root, return_to,
                                         form_tag_attrs=form_tag)

    def auth_complete(self, *args, **kwargs):
        response = self.consumer().complete(dict(self.data.items()),
                                            self.request.build_absolute_uri())
        if not response:
            raise ValueError('This is an OpenID relying party endpoint')
        elif response.status == SUCCESS:
            kwargs.update({'response': response, self.AUTH_BACKEND.name: True})
            return authenticate(*args, **kwargs)
        elif response.status == FAILURE:
            raise ValueError('OpenID authentication failed: %s' % \
                             response.message)
        elif response.status == CANCEL:
            raise ValueError('Authentication cancelled')
        else:
            raise ValueError('Unknown OpenID response type: %r' % \
                             response.status)

    def setup_request(self):
        """Setup request"""
        openid_request = self.openid_request()
        # Request some user details. Use attribute exchange if provider
        # advertises support.
        if openid_request.endpoint.supportsType(ax.AXMessage.ns_uri):
            fetch_request = ax.FetchRequest()
            # Mark all attributes as required, Google ignores optional ones
            for attr, alias in (AX_SCHEMA_ATTRS + OLD_AX_ATTRS):
                fetch_request.add(ax.AttrInfo(attr, alias=alias,
                                              required=True))
        else:
            fetch_request = sreg.SRegRequest(optional=dict(SREG_ATTR).keys())
        openid_request.addExtension(fetch_request)

        return openid_request

    def consumer(self):
        """Create an OpenID Consumer object for the given Django request."""
        return Consumer(self.request.session.setdefault(SESSION_NAME, {}),
                        DjangoOpenIDStore())

    @property
    def uses_redirect(self):
        """Return true if openid request will be handled with redirect or
        HTML content will be returned.
        """
        if not hasattr(self, '_uses_redirect'):
            setattr(self, '_uses_redirect',
                    self.openid_request().shouldSendRedirect())
        return getattr(self, '_uses_redirect', True)

    def openid_request(self):
        """Return openid request"""
        if not hasattr(self, '_openid_request'):
            openid_url = self.openid_url()
            try:
                openid_request = self.consumer().begin(openid_url)
            except DiscoveryFailure, err:
                raise ValueError('OpenID discovery error: %s' % err)
            else:
                setattr(self, '_openid_request', openid_request)
        return getattr(self, '_openid_request', None)

    def openid_url(self):
        """Return service provider URL.
        This base class is generic accepting a POST parameter that specifies
        provider URL."""
        if OPENID_ID_FIELD not in self.data:
            raise ValueError('Missing openid identifier')
        return self.data[OPENID_ID_FIELD]


class BaseOAuth(BaseAuth):
    """OAuth base class"""
    def __init__(self, request, redirect):
        """Init method"""
        super(BaseOAuth, self).__init__(request, redirect)
        self.redirect_uri = self.request.build_absolute_uri(self.redirect)


class ConsumerBasedOAuth(BaseOAuth):
    """Consumer based mechanism OAuth authentication, fill the needed
    parameters to communicate properly with authentication service.

        @AUTHORIZATION_URL       Authorization service url
        @REQUEST_TOKEN_URL       Request token URL
        @ACCESS_TOKEN_URL        Access token URL
        @SERVER_URL              Authorization server URL
    """
    AUTHORIZATION_URL = ''
    REQUEST_TOKEN_URL = ''
    ACCESS_TOKEN_URL = ''
    SERVER_URL = ''
    SETTINGS_KEY_NAME = ''
    SETTINGS_SECRET_NAME = ''

    def auth_url(self):
        """Returns redirect url"""
        token = self.unauthorized_token()
        name = self.AUTH_BACKEND.name + 'unauthorized_token_name'
        self.request.session[name] = token.to_string()
        return self.oauth_request(token, self.AUTHORIZATION_URL).to_url()

    def auth_complete(self, *args, **kwargs):
        """Returns user, might be logged in"""
        name = self.AUTH_BACKEND.name + 'unauthorized_token_name'
        unauthed_token = self.request.session.get(name)
        if not unauthed_token:
            raise ValueError('Missing unauthorized token')

        token = OAuthToken.from_string(unauthed_token)
        if token.key != self.data.get('oauth_token', 'no-token'):
            raise ValueError('Incorrect tokens')

        access_token = self.access_token(token)
        data = self.user_data(access_token)
        if data is not None:
            data['access_token'] = access_token.to_string()

        kwargs.update({'response': data, self.AUTH_BACKEND.name: True})
        return authenticate(*args, **kwargs)

    def unauthorized_token(self):
        """Return request for unauthorized token (first stage)"""
        request = self.oauth_request(token=None, url=self.REQUEST_TOKEN_URL)
        response = self.fetch_response(request)
        return OAuthToken.from_string(response)

    def oauth_request(self, token, url, extra_params=None):
        """Generate OAuth request, setups callback url"""
        params = {'oauth_callback': self.redirect_uri}
        if extra_params:
            params.update(extra_params)

        if 'oauth_verifier' in self.data:
            params['oauth_verifier'] = self.data['oauth_verifier']
        request = OAuthRequest.from_consumer_and_token(self.consumer,
                                                       token=token,
                                                       http_url=url,
                                                       parameters=params)
        request.sign_request(OAuthSignatureMethod_HMAC_SHA1(), self.consumer,
                             token)
        return request

    def fetch_response(self, request):
        """Executes request and fetchs service response"""
        self.connection.request(request.http_method, request.to_url())
        response = self.connection.getresponse()
        return response.read()

    def access_token(self, token):
        """Return request for access token value"""
        request = self.oauth_request(token, self.ACCESS_TOKEN_URL)
        return OAuthToken.from_string(self.fetch_response(request))

    def user_data(self, access_token):
        """Loads user data from service"""
        raise NotImplementedError('Implement in subclass')

    @property
    def connection(self):
        """Setups connection"""
        conn = getattr(self, '_connection', None)
        if conn is None:
            conn = HTTPSConnection(self.SERVER_URL)
            setattr(self, '_connection', conn)
        return conn

    @property
    def consumer(self):
        """Setups consumer"""
        cons = getattr(self, '_consumer', None)
        if cons is None:
            cons = OAuthConsumer(*self.get_key_and_secret())
            setattr(self, '_consumer', cons)
        return cons

    def get_key_and_secret(self):
        """Return tuple with Consumer Key and Consumer Secret for current
        service provider. Must return (key, secret), order *must* be respected.
        """
        return getattr(settings, self.SETTINGS_KEY_NAME), \
               getattr(settings, self.SETTINGS_SECRET_NAME)

    @classmethod
    def enabled(cls):
        """Return backend enabled status by checking basic settings"""
        return all(hasattr(settings, name) for name in
                        (cls.SETTINGS_KEY_NAME,
                         cls.SETTINGS_SECRET_NAME))


# import sources from where check for auth backends
SOCIAL_AUTH_IMPORT_SOURCES = (
    'social_auth.backends',
    'social_auth.backends.contrib',
) + getattr(settings, 'SOCIAL_AUTH_IMPORT_BACKENDS', ())

def get_backends():
    backends = {}

    for mod_name in SOCIAL_AUTH_IMPORT_SOURCES:
        try:
            mod = import_module(mod_name)
        except ImportError:
            continue

        for directory, subdir, files in walk(mod.__path__[0]):
            for name in filter(lambda name: name.endswith('.py'), files):
                try:
                    name = basename(name).replace('.py', '')
                    sub = import_module(mod_name + '.' + name)
                    # register only enabled backends
                    backends.update(((key, val)
                                        for key, val in sub.BACKENDS.items()
                                            if val.enabled()))
                except (ImportError, AttributeError):
                    pass
    return backends

# load backends from defined modules
BACKENDS = get_backends()
BACKENDS[OpenIdAuth.AUTH_BACKEND.name] = OpenIdAuth

def get_backend(name, *args, **kwargs):
    """Return auth backend instance *if* it's registered, None in other case"""
    return BACKENDS.get(name, lambda *args, **kwargs: None)(*args, **kwargs)
