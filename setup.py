#!/usr/bin/env python
# encoding=utf-8
from setuptools import setup, find_packages
import os, sys

setup(name='ukbot',
      version='1.1.0',
      description='Wikipedia writing contest bot',
      keywords='wikipedia',
      author='Dan Michael O. Heggø',
      author_email='danmichaelo@gmail.com',
      url='https://github.com/wikimedianorge/ukbot',
      license='MIT',
      packages=find_packages(),
      include_package_data=True,
      entry_points={
        'console_scripts': [
            'ukbot = ukbot.ukbot:main',
        ],
      },
      classifiers=[
        'Programming Language :: Python',
        'Programming Language :: Python :: 3.9',
      ],
      install_requires=[
        'Jinja2',
        'Werkzeug',
        'pytz',
        'isoweek',
        'pyyaml',
        'jsonpath-rw',
        'lxml',
        'beautifulsoup4',
        'numpy',
        'matplotlib',
        'mwclient',
        'mwtemplates',
        'mwtextextractor',
        'rollbar',
        'flipflop',
        'flask',
        'requests',
        'pymysql',
        'psutil',
        'python-dotenv',
        'pydash',
        'retry',
        'more-itertools',
      ])
