#!/usr/bin/env python
# encoding: utf-8

from os import path

DATABASES = {
	'default': {
		'ENGINE': 'django.db.backends.sqlite3',
		'NAME': 'transphorm',
		'USER': '',
		'PASSWORD': '',
		'HOST': '',
		'PORT': '',
	}
}

TWITTER_CONSUMER_KEY     = 'BZyWOIVYcVuiZqiKq0JoSw'
TWITTER_CONSUMER_SECRET  = '4SR2r8PNQ5JH4Dgpa6uYexxesDGBaZFRzOA72PFPeBI'
#FACEBOOK_APP_ID          = ''
#FACEBOOK_API_SECRET      = ''

DEBUG = False
SITE_ROOT = path.abspath(path.dirname(__file__) + '/../')
