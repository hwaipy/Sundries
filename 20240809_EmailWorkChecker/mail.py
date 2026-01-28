import email.utils
import imaplib
import email
from email import policy
import datetime
import time
import hashlib
import os
import GLM


class Mailbox:
  def __init__(self, server, username, password, cachePath):
    self.server = server
    self.username = username
    self.password = password
    self.cachePath = cachePath
    self.imap = imaplib.IMAP4_SSL(server)
    self.imap.login(username, password)
    self.currentFolder = None

  def selectFolder(self, folder):
    if folder == self.currentFolder: return
    self.imap.select(folder)
    self.currentFolder = folder

  def search(self, folder, query):
    self.selectFolder(folder)
    status, messages = self.imap.search(None, *query)
    if status != 'OK': raise ValueError
    if len(messages[0]) == 0: return []
    messages = messages[0].split(b' ')
    return messages

  def specifiedSearch(self, folder, since=None, to=None, froms=None):
    assert type(since) == str
    query = []
    if since:
      query.append(f'SINCE {datetime.date.fromisoformat(since).strftime("%d-%b-%Y")}')
    if to:
      query.append(self.__createAddressList('OR', 'TO', to))
    if froms:
      query.append(self.__createAddressList('OR', 'FROM', froms))
    messages = self.search(folder, query)
    return [Email(self, folder, m) for m in messages]

  def __createAddressList(self, condition, key, addrs):
    if type(addrs) != list:
      addrs = [addrs]
    addrs = [f'{key} "{a}"' for a in addrs]
    q = addrs[0]
    for a in addrs[1:]:
      q = f'{condition} ({q}) {a}'
    return q

  def fetch(self, folder, id, query):
    self.selectFolder(folder)
    return self.imap.fetch(id, query)[1][0][1]


class Email:
  def __init__(self, mailbox, folder, id):
    self.mailbox = mailbox
    self.folder = folder
    self.id = id
    self.subject = None
    self.fromAddr = None
    self.toAddrs = []
    self.ccAddrs = []
    self.date = None
    self.bytes = None
    self.body = None

  def fetchMetaInfo(self):
    message = self.mailbox.fetch(self.folder, self.id, "(BODY[HEADER.FIELDS (FROM TO SUBJECT CC DATE MESSAGE-ID)])")
    message = email.message_from_bytes(message)
    if message['SUBJECT']:
      subject, encoding = email.header.decode_header(message['SUBJECT'])[0]
      if isinstance(subject, bytes):
        subject = Util.tryDecode(subject, priorityCharset=encoding)
      self.subject = subject
    if message['FROM']:
      if isinstance(message['FROM'], email.header.Header): raise ValueError('Non ASCII address of FROM')
      self.fromAddr = email.utils.parseaddr(message['FROM'])[1]
    if message['TO']:
      self.toAddrs = [email.utils.parseaddr(s.strip())[1] for s in message['TO'].split(',')]
    if message['CC']:
      self.ccAddrs = [email.utils.parseaddr(s.strip())[1] for s in message['CC'].split(',')]
    if message['Date']:
      self.date = email.utils.parsedate_to_datetime(email.header.decode_header(message['Date'])[0][0])

  def __getEmailUid(self):
    uidString = f'{self.date} {self.subject} {self.fromAddr} {self.toAddrs} {self.ccAddrs}'
    uid = hashlib.md5(uidString.encode()).hexdigest()
    return uid

  def fetchBody(self, cache=True):
    if cache:
      uid = self.__getEmailUid()
      cacheFile = f'{self.mailbox.cachePath}/{uid}.email'
      if os.path.exists(cacheFile):
        with open(cacheFile, 'rb') as f:
          self.bytes = f.read()
    if not self.bytes:
      self.bytes = self.mailbox.fetch(self.folder, self.id, "RFC822")
      if cache:
        with open(cacheFile, 'wb') as f:
          f.write(self.bytes)
    message = email.message_from_bytes(self.bytes, policy=policy.default)
    self.body = EmailPart(message)

  def summary(self):
    s = '----------------------------------------------------------------------------------------------------------------------------------------------------------------\n'
    s += f'Size: {len(self.bytes)}\n'
    s += f'Subject: {self.subject}\n'
    s += f'Date: {self.date}\n'
    s += f'From: {self.fromAddr}\n'
    s += f'To: {self.toAddrs}\n'
    s += f'CC: {self.ccAddrs}\n'
    s += '\n'

    def summaryPart(part, deepth):
      sp = ''
      for i in range(deepth):
        sp += ' - '

      sp += f'Type[{part.type}] '
      if part.type == 'text/plain':
        sp += part.content.replace('\n', '\\n').replace('\r', '\\r')[:100]
      if part.type == 'text/html':
        sp += part.content.replace('\n', '\\n')[:100]
      if part.type == 'application/octet-stream':
        assert part.isAttachment
        sp += f'{len(part.attachmentContent)}  {part.attachmentFilename}'
      else:
        # raise ValueError(f'{part.type} not valid.')
        pass
      sp += '\n'

      for subPart in part.subParts:
        sp += summaryPart(subPart, deepth + 1)
      return sp

    if self.body: s += summaryPart(self.body, 0)
    s += '================================================================================================================================================================\n'

    return s


class EmailPart:
  def __init__(self, message):
    self.message = message
    self.subParts = []
    self.type = self.message.get_content_type()
    self.content = self.message.get_payload(None, decode=True)
    self.contentCharset = self.message.get_content_charset()
    self.fileName = self.message.get_filename()
    if self.content is not None and self.type in ['text/plain', 'text/html']: self.content = Util.tryDecode(self.content, priorityCharset=self.contentCharset)
    self.isAttachment = self.message.is_attachment()
    self.attachmentFilename = None
    self.attachmentContent = None
    if self.isAttachment:
      self.attachmentFilename = self.message.get_filename()
      self.attachmentContent = self.content
    self.subParts = [] if not self.message.is_multipart() else [EmailPart(part) for part in self.message.get_payload()]


class Util:
  @classmethod
  def tryDecode(self, bytes, possibleCharsets=['UTF-8', 'GB2312', 'GBK', 'GB18030'], priorityCharset=None):
    possibleCharsets = [cs.upper() for cs in possibleCharsets]
    if priorityCharset: priorityCharset = priorityCharset.upper()
    charsets = ([priorityCharset] if priorityCharset else []) + [cs for cs in possibleCharsets if cs != priorityCharset]
    for charset in charsets:
      try:
        return bytes.decode(charset)
      except BaseException as e:
        pass
    raise ValueError('Can not decode.')
  
def checkSubjectType(subject):
  validTypes = ['Purchase', 'Employment', 'Submission', 'Other']
  for i in range(5):
    response = GLM.simpleQA(f'我收到一封邮件，请帮我判断这封邮件属于什么类型，并用一个单词来回答该问题。如果你觉得它属于采购申请，请回答Purchase；如果你觉得它属于聘用申请，请回答Employment；如果你觉得它属于论文投稿申请，请回答Submission；如果你觉得它不是上述类型，或无法判断其类型，请回答Other。请务必只回答一个单词，这个单词只能是"Purchase","Employment","Submission",或"Other"。邮件的主题为："{subject}"')
    print(response)
    # check if it responsed a certain type
    match = [(type.lower() in response.lower()) for type in validTypes]
    if len([m for m in match if m]) == 1:
      return validTypes[match.index(True)]
  return 'Failed'

if __name__ == '__main__':
  t0 = time.time()

  from _data.config import config
  mailbox = Mailbox(server=config['server'], username=config['username'], password=config['password'], cachePath='_data')
  validApplyers = config['validApplyers']
  validReceiver = config['validReceiver']
  since = '2014-07-05'
  messages = mailbox.specifiedSearch('Archive', since=since, to=validReceiver, froms=validApplyers)
  print(f'There are {len(messages)} emails in total.')

  t1 = time.time()
  for message in messages:
    message.fetchMetaInfo()
    print(message.date, message.subject, message.fromAddr, message.toAddrs, message.ccAddrs)
    # print("::::::::::::::::::::" + checkSubjectType(message.subject))
  t2 = time.time()
  print(f'All metainfo of {len(messages)} emails loaded in {t2 - t1} s.')

  purchaseApplications = [message for message in messages if '申请' in message.subject and '采购' in message.subject]
  print(f'{len(purchaseApplications)} purchase applications emails got.')
  print()

  for message in messages:
    message.fetchBody()
    print(message.summary())

