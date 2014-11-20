from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from pubnub import Pubnub
from datetime import datetime

import os, base64, requests, json

from Yowsup.connectionmanager import YowsupConnectionManager

from models import Account, Message, Job
from util import get_phone_number, error_message, utc_to_local


class Client:
	def __init__(self, phone_number, logger):
		self.phone_number = phone_number
		self.logger = logger
		self.url = os.environ['URL']

		self.init_db()

		self.connected = False
		self.connectionManager = YowsupConnectionManager()
		self.connectionManager.setAutoPong(True)		

		self.signalsInterface = self.connectionManager.getSignalsInterface()
		self.methodsInterface = self.connectionManager.getMethodsInterface()

		self._registerListeners()
		self._initRealtime()

	def init_db(self):
		url = os.environ['SQLALCHEMY_DATABASE_URI']
		self.db = create_engine(url, echo=False, pool_size=5, pool_timeout=600,pool_recycle=600)
		self.s = sessionmaker(bind=self.db)
		# self.session = self.s()

		_session = self.s()
		self.account = _session.query(Account).filter_by(phone_number = self.phone_number).scalar()
		_session.commit()
		self.password = self.account.whatsapp_password

	def connect(self):
		self.logger.info("Connecting")		
		self.methodsInterface.call("auth_login", (self.phone_number, base64.b64decode(bytes(self.account.whatsapp_password.encode('utf-8')))))
		self.connected = True

	def disconnect(self):
		self._setStatus(0, "Disconected!")

	def _registerListeners(self):
		self.signalsInterface.registerListener("auth_success", self._onAuthSuccess)
		self.signalsInterface.registerListener("auth_fail", self._onAuthFailed)
		self.signalsInterface.registerListener("disconnected", self._onDisconnected)
		self.signalsInterface.registerListener("message_received", self._onMessageReceived)
		self.signalsInterface.registerListener("group_messageReceived", self._onGroupMessageReceived)
		self.signalsInterface.registerListener("receipt_messageDelivered", self._onReceiptMessageDelivered)
		self.signalsInterface.registerListener("image_received", self._onImageReceived)

	def work(self):
		if self.connected:
			self._i("About to begin work - sent: %s, account_id: %s, pending: %s" %(False, self.account.id, False))
			_session = self.s()
			jobs = _session.query(Job).filter_by(sent=False, account_id=self.account.id, pending=False).all()
			self._i("Number of jobs %s" % len(jobs))
			

			for job in jobs:
				self._d("Processing job %s" %job.method)
				if self._onSchedule(job.scheduled_time):
					self._i("Job %s-%s can run" %(job.id, job.method))
					self._do(job)

			_session.commit()

	# work methods

	def _do(self,job):
		success = False
		if job.method == "sendMessage":
			success = self._sendMessage(job)

		if success:
			job.sent = success
			job.runs += 1

			# self.session.commit()

	def _sendMessage(self, job):
		to = job.targets
		text = job.args
		self._d("Sending %s to %s" %(text, to))
		message_id = self.methodsInterface.call("message_send", (to, text))	
		job.whatsapp_message_id = message_id
		return True

	# util methods

	def _post(self, url, data):
		data.update(account = self.phone_number)
		headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
		r = requests.post(self.url + url, data=json.dumps(data), headers=headers)

	def _patch(self,url,data):
		data.update(account = self.phone_number)
		headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
		r = requests.patch(self.url + url, data=json.dumps(data), headers=headers)

	def _setStatus(self, status, message="Connected!"):
		self.logger.info("Setting status %s" %status)
		data = { "status" : status, "message" : message }
		self._post("/status", data)

	def _messageExists(self, whatsapp_message_id):
		_session = self.s()
		message = _session.query(Message).filter_by(whatsapp_message_id=whatsapp_message_id, account_id=self.account.id).scalar()
		_session.commit()
		return message is not None

	def _sendRealtime(self, message):
		if self.use_realtime:
			self.pubnub.publish({
				'channel' : self.channel,
				'account' : self.phone_number,
				'message' : message
			})

	def _initRealtime(self):
		self.channel = os.environ['PUB_CHANNEL'] + "_%s" %self.phone_number
		self.use_realtime = True
		self.pubnub = Pubnub(os.environ['PUB_KEY'], os.environ['SUB_KEY'], None, False)

	def _onSchedule(self,scheduled_time):
		return (scheduled_time is None or datetime.now() > utc_to_local(scheduled_time))


	def _d(self, message):
		self.logger.debug(message)

	def _i(self, message):
		self.logger.info(message)

	def _e(self, message):
		self.logger.error(message)

	def _w(self, message):
		self.logger.warning(message)

	# signals

	def _onImageReceived(self, messageId, jid, preview, url, size, wantsReceipt, isBroadCast):	
		self._d('Image Received')	
		phone_number = get_phone_number(jid)

		data = { "message" : { 'url' : url, 'message_type' : 'Image' , 'phone_number' : phone_number, "whatsapp_message_id" : messageId, 'name' : '' } }
		self._post("/upload", data)

		# send receipts
		self.methodsInterface.call("message_ack", (jid, messageId))

		self._sendRealtime({
			'type' : 'image',
			'phone_number' : phone_number,
			'url' : url,
			'name' : ''
		})

	def _onReceiptMessageDelivered(self, jid, messageId):
		self._d("Delivered %s from %s" %(messageId, jid))		
				
		_session = self.s()
		job = _session.query(Job).filter_by(sent=True, whatsapp_message_id=messageId, account_id=self.account.id).scalar()
		if job is not None:
			job.received = True
			# self.session.commit()

			if job.method == "sendMessage":
				m = _session.query(Message).get(job.message_id)
				self._d("Looking for message with id to send a receipt %s" %job.message_id)
				if m is not None:
					m.received = True
					m.receipt_timestamp = datetime.now()
															
					data = { "receipt" : { "message_id" : m.id } }
					self._post("/receipt", data)

					self._sendRealtime({
						'type' : 'receipt',
						'message_id': m.id
					})
			else:
				data = { "receipt" : { "message_id" : messageId, "phone_number" : jid.split("@")[0] } }
				self._post("/broadcast_receipt", data)
		_session.commit()

	def _onGroupMessageReceived(self, messageId, jid, author, content, timestamp, wantsReceipt, pushName):
		self._i('Received a message on the group %s' %content)
		self._i('JID %s - %s - %s' %(jid, pushName, author))
		
		if self._messageExists(messageId) == False:

			# Always send receipts
			self.methodsInterface.call("message_ack", (jid, messageId))
			
			# Post to server
			data = { "message" : { "text" : content, "group_jid" : jid, "message_type" : "Text", "whatsapp_message_id" : messageId, "name" : pushName, "jid" : author }}
			self._post("/receive_broadcast", data)

		else:
			self._w("Duplicate group message (%s) %s - %s" %(messageId, self.phone_number, self.account.name))
			rollbar.report_message('Duplicate group message (%s) %s - %s' %(messageId, self.phone_number, self.account.name), 'warning')

	def _onMessageReceived(self, messageId, jid, messageContent, timestamp, wantsReceipt, pushName, isBroadcast):
		phone_number = get_phone_number(jid)
		self._d("Received message %s from %s - %s" %(messageContent, phone_number, pushName))
		if self._messageExists(messageId) == False:
			
			# Always send receipts
			self.methodsInterface.call("message_ack", (jid, messageId))

			# Post to server
			data = { "message" : { "text" : messageContent, "phone_number" : phone_number, "message_type" : "Text", "whatsapp_message_id" : messageId, "name" : pushName  }}
			self._post("/messages", data)

			self._sendRealtime({
				'type' : 'text',
				'phone_number' : phone_number,
				'text' : messageContent,
				'name' : pushName
			})
		else:
			self._w("Duplicate message %s" %messageId)			
		
	def _onAuthSuccess(self, username):
		self.logger.info("Auth Success! - %s" %username)
		self.methodsInterface.call("ready")
		self.methodsInterface.call("clientconfig_send")
		self.methodsInterface.call("presence_sendAvailable", ())
		self._setStatus(1)
		self.connected = True

	def _onAuthFailed(self, username, err):
		self.logger.error("Auth error! - %s. Using %s with %s " %(err, username, self.password))
		self._post("/wa_auth_error", {})

	def _onDisconnected(self, reason):
		self._e("Disconected! - %s, %s" %(self.phone_number, reason))
		self.connected = False
		error_message("Unscheduled disconnect for %s" %self.phone_number, "warning")
