import logging.handlers
from .webinterface.app import app

logging.basicConfig()

logger = logging.getLogger()
logger.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s %(levelname)s] %(message)s')

logger.info('ukbot webserver started')

fh = logging.StreamHandler()
fh.setLevel(logging.DEBUG)
fh.setFormatter(formatter)
app.logger.addHandler(fh)

# app.debug = True  # reload on each code change
