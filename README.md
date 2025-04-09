<h1 align="center">UKBot</h1>

<p align="center">Bot for updating results in writing contests at Wikipedia, deployed at <a href="https://ukbot.toolforge.org/">ukbot.toolforge.org</a>.</p>

<div align="center">
  <p>Create with ❤︎ by <a href="https://github.com/WikimediaNorge/UKBot/graphs/contributors">contributors</a></p>
</div>
 
## Getting Started

Clone code from github

	git clone https://github.com/WikimediaNorge/UKBot.git
	cd UKBot

Create a new Python3 virtualenv and activate it:

	mkdir -p www/python/venv
	python3 -m venv www/python/venv
	. www/python/venv/bin/activate

Install dependencies:

	pip install .

If installation fails on Mac, try

	LDFLAGS=-L/opt/homebrew/lib pip install .

Generate locales:

    make all

Start a MariaDB instance with the necesseary database tables:

	docker compose up -d

Create a configuration file:

	cp .env.dist .env

and modify it if needed. The default database credentials should work with the MariaDB instance from Docker, but you may need to add Wikimedia credentials ([Oauth 1.0a consumer-only credentials](https://meta.wikimedia.org/wiki/Special:OAuthConsumerRegistration/propose?wpownerOnly=1&wpoauthVersion=2))
if you want to actually run the bot locally.

Within the virtualenv you should now be able to run the bot. For testing purposes, you can create
a sandbox contest page such as this one: https://no.wikipedia.org/wiki/Bruker:Danmichaelo/Sandkasse5
and test the bot with that page:

	ukbot --page Bruker:Danmichaelo/Sandkasse5 --simulate config/config.no-mk.yml

or using the wrapper script:

	JOB_NAME=no-mk JOB_ID=adhoc1 ./jobs/run.sh --page Bruker:Danmichaelo/Sandkasse5

To test the webinterface locally:

	FLASK_UWSGI_DEBUG=true uwsgi --ini uwsgi.dev.ini --python-autoreload 1 --master --http :5000

To modify the code and test it locally, you'll need to install it as a pip package first, and then run it:

	pip install .
	ukbot --page Bruker:Danmichaelo/Sandkasse5 --simulate config/config.no-mk.yml




## Deployment

The bot is deployed at [ToolForge](https://wikitech.wikimedia.org/wiki/Portal:Toolforge) under the "UKBot" account using 
[Webservice](https://wikitech.wikimedia.org/wiki/Help:Toolforge/Web) and the [Toolforge Jobs Framework](https://wikitech.wikimedia.org/wiki/Portal:Toolforge/Admin/Kubernetes/Jobs_framework). 

See https://wikitech.wikimedia.org/wiki/Tool:UKBot for deployment notes.

## Other notes

Forenklet flytkart:
![Flowchart](https://github.com/danmichaelo/UKBot/raw/master/flowchart.png)
 
