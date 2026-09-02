"""
Django settings for config project.
"""

import os
from pathlib import Path

from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = config(
    "DJANGO_SECRET_KEY",
    default="django-insecure-5#x3r9*v53&l5@p2+fy4g9qcib1@2bqkre(@88po6!_022)3#!",
)

DEBUG = config("DJANGO_DEBUG", default=True, cast=bool)

ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())


# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.humanize',
    'tracker',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME', default='app_facturacion'),
        'USER': config('DB_USER', default=os.getenv('USER', 'postgres')),
        'PASSWORD': config('DB_PASSWORD', default=''),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default='5432'),
    }
}


# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

LANGUAGE_CODE = 'es-mx'

TIME_ZONE = 'America/Mexico_City'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LOGIN_URL = 'tracker:login'
LOGIN_REDIRECT_URL = 'tracker:queue'
LOGOUT_REDIRECT_URL = 'tracker:login'


# Email — console backend for now; the flows notify requesters/assistants by
# email in the design, but no real mail server is wired up yet.
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


# KPI targets and bonus formula — see tracker/kpi.py. Kept here so the
# business rules the design assumed (48h/2h/8h/2%, weighted score, base
# bonus) are configurable without touching code.
KPI_SETTINGS = {
    "PO_TARGET_HOURS": config("KPI_PO_TARGET_HOURS", default=48, cast=int),
    "IN_PROCESS_TARGET_HOURS": config("KPI_IN_PROCESS_TARGET_HOURS", default=2, cast=int),
    "INVOICE_TARGET_HOURS": config("KPI_INVOICE_TARGET_HOURS", default=8, cast=int),
    "ERROR_RATE_TARGET": config("KPI_ERROR_RATE_TARGET", default=0.02, cast=float),
    "PO_ON_TIME_TARGET": config("KPI_PO_ON_TIME_TARGET", default=0.90, cast=float),
    "IN_PROCESS_ON_TIME_TARGET": config("KPI_IN_PROCESS_ON_TIME_TARGET", default=0.95, cast=float),
    "INVOICE_ON_TIME_TARGET": config("KPI_INVOICE_ON_TIME_TARGET", default=0.90, cast=float),
    "WEIGHT_PO_ON_TIME": config("KPI_WEIGHT_PO_ON_TIME", default=0.30, cast=float),
    "WEIGHT_IN_PROCESS_ON_TIME": config("KPI_WEIGHT_IN_PROCESS_ON_TIME", default=0.20, cast=float),
    "WEIGHT_INVOICE_ON_TIME": config("KPI_WEIGHT_INVOICE_ON_TIME", default=0.25, cast=float),
    "WEIGHT_ERROR_RATE": config("KPI_WEIGHT_ERROR_RATE", default=0.25, cast=float),
    "BONUS_THRESHOLD": config("KPI_BONUS_THRESHOLD", default=0.70, cast=float),
    # Aspirational mark on the attainment gauge itself — distinct from the
    # per-indicator on-time targets above (see kpi_scorecard.html).
    "ATTAINMENT_TARGET": config("KPI_ATTAINMENT_TARGET", default=0.90, cast=float),
    "BASE_BONUS": config("KPI_BASE_BONUS", default=4000, cast=int),
}
