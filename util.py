import os
import rollbar
import logging


rollbar.init(os.environ['ROLLBAR_KEY'], os.environ['ENV'])

def error_message(message, type='warning'):
	rollbar.report_message(message, type)

def _d(self, message, account=None, debug=False):
	if debug == False:
		logging.info("%s - %s" %(self.username, message))
	else:
		print("%s - %s" %(account, message))
		logging.debug("%s - %s" %(account, message))	

def setup_logging(phone_number, env):
	logger = logging.getLogger(__name__)
	logger.setLevel(logging.DEBUG)
	
	handler = logging.FileHandler("logs/%s.%s.log" %(phone_number, env))
	handler.setLevel(logging.INFO)

	formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
	handler.setFormatter(formatter)

	# add the handlers to the logger

	logger.addHandler(handler)
	return logger

def get_phone_number(jid):
	phone_number = jid.split("@")[0]
	return phone_number