# vim: fileencoding=utf8

"""
Drives a browser to search for tickets across multiple airline sites,
scraping/emailing/plotting fare information.
"""

from selenium import webdriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException
import cPickle as pickle, cStringIO as StringIO, argparse, contextlib, \
    datetime as dt, functools, getpass, logging, ludibrio, os, re, smtplib, \
    socket, subprocess, sys, time, pprint, calendar, collections, urllib
from email.mime.text import MIMEText
import dateutil.relativedelta as rd, ipdb, pyjade, pyjade.ext.html
from parsedatetime import parsedatetime as pdt, parsedatetime_consts as pdc

class html_compiler(pyjade.ext.html.HTMLCompiler):
  def visitCode(self, code):
    if not code.buffer and not code.block:
      exec code.val.lstrip() in self.global_context, self.local_context
    pyjade.ext.html.HTMLCompiler.visitCode(self, code)

date_parser = pdt.Calendar(pdc.Constants())

def month_of(date): return date + rd.relativedelta(day=1)
def fmt_date(date, short=False):
  return date.strftime('%m/%d/%Y') if not short else \
      '%s/%s/%s' % (date.month, date.day, date.year)
day_names = set.union(set([
  x.lower() for xs in calendar.day_name,calendar.day_abbr for x in xs]))
space = re.compile(r'\s+')
def parse_date(text):
  text = text.strip()
  if space.split(text, 1)[0].lower() in day_names:
    text = space.split(text, 1)[1]
  return dt.date(*date_parser.parse(text)[0][:3])

def retry_if_nexist(multireturn=False):
  def dec(f):
    @functools.wraps(f)
    def wrapper(self, x, retry = True, maxsec = 60, dummy = True, permit_none = False):
      start = time.time()
      while 1:
        try:
          res = f(self, x)
          if multireturn and not permit_none and res == []:
            raise NoSuchElementException()
          return res
        except NoSuchElementException:
          if not retry: return ludibrio.Dummy() if dummy else None
          if time.time() - start > maxsec: raise timeout_exception()
          time.sleep(1)
    return wrapper
  return dec

class timeout_exception(Exception): pass

def retry_if_timeout(f):
  @functools.wraps(f)
  def wrapper(wd, *args, **kw):
    while 1:
      try: return f(wd, *args, **kw)
      except timeout_exception: time.sleep(1)
      except Exception as ex:
        if wd.debug:
          ipdb.post_mortem(sys.exc_info()[2])
        raise
  return wrapper

class rich_driver(object):
  def __init__(self, wd, debug):
    self.wd = wd
    self.debug = debug
  def __getattr__(self, attr): return getattr(self.wd, attr)
  def ckpt(self):
    """Callback from an airline function after filling but before submitting
    the form.  Useful if you want to take a screenshot, make some edits,
    etc."""
    pass
  @retry_if_nexist()
  def xpath(self, x): return rich_web_elt(self.wd.find_element_by_xpath(x))
  @retry_if_nexist(True)
  def xpaths(self, x): return map(rich_web_elt, self.wd.find_elements_by_xpath(x))
  @retry_if_nexist()
  def getid(self, x): return rich_web_elt(self.wd.find_element_by_id(x))
  @retry_if_nexist()
  def name(self, x): return rich_web_elt(self.xpath('//*[@name=%r]' % (x,)))
  @retry_if_nexist()
  def css(self, x): return rich_web_elt(self.wd.find_element_by_css_selector(x))
  @retry_if_nexist(True)
  def csss(self, x): return map(rich_web_elt, self.wd.find_elements_by_css_selector(x))

price_re = re.compile(r'\d+')
def toprc(x):
  return int(price_re.search(x.text
                        if type(x) is rich_web_elt or type(x) is WebElement
                        else x).group())

class rich_web_elt(object):
  def __init__(self, elt):
    self.elt = elt
  def clear(self):
    self.elt.clear()
    return self
  def click(self):
    self.elt.click()
    return self
  def send_keys(self, keys):
    self.elt.send_keys(keys)
    return self
  def delay(self, delay = 1):
    time.sleep(delay)
    return self
  def tab(self):
    self.elt.send_keys(Keys.TAB)
    return self
  def enter(self):
    self.elt.send_keys(Keys.ENTER)
    return self
  def option(self, val):
    self.elt.find_element_by_xpath('option[@value=%r]' % str(val)).click()
    return self
  def set(self, value):
    if self.elt.is_selected() != value:
      self.elt.click()
    return self
  def slow_keys(self, keys):
    for k in keys:
      self.send_keys(k)
      time.sleep(.1)
    return self
  def wait_displayed(self, sleep=1, max=20):
    start = time.time()
    while time.time() - start < max and not self.elt.is_displayed():
      time.sleep(sleep)
    return self
  def __getattr__(self, attr):
    return getattr(self.elt, attr)

def fullcity(tla):
  return dict(ewr = 'Newark',
      sfo = 'San Francisco',
      phl = 'Philadelphia',
      oak = 'Oakland',
      sjc = 'San Jose')[tla.lower()]

@retry_if_timeout
def united(wd, org, dst, date, nearby=False):
  """
  Returns list of (best price, day) pairs for month around date.
  """
  wd.get('http://united.com')
  wd.getid('ctl00_ContentInfo_Booking1_rdoSearchType2').click()
  wd.getid('ctl00_ContentInfo_Booking1_Origin_txtOrigin').clear().send_keys(org)
  wd.getid('ctl00_ContentInfo_Booking1_Destination_txtDestination').clear().send_keys(dst)
  if nearby:
    wd.getid('ctl00_ContentInfo_Booking1_Nearbyair_chkFltOpt').set(True)
  wd.getid('ctl00_ContentInfo_Booking1_AltDate_chkFltOpt').set(True)
  wd.getid('ctl00_ContentInfo_Booking1_DepDateTime_rdoDateFlex').click()
  wd.getid('ctl00_ContentInfo_Booking1_DepDateTime_MonthList1_cboMonth').option(fmt_date(month_of(date), True)).click()
  wd.ckpt()
  wd.getid('ctl00_ContentInfo_Booking1_btnSearchFlight').click()
  def gen():
    for x in wd.find_elements_by_css_selector('.on'):
      date, _, prc = x.text.split('\n')
      yield toprc(prc), parse_date(date)
  return list(gen())

@retry_if_timeout
def aa(wd, org, dst, date, dist_org=0, dist_dst=0):
  """
  dist_org and dist_dst are either 0, 30, 60, or 90 (miles).

  Returns list of (best price, day) pairs for +/- 3 days around date.
  """
  for dist in dist_org, dist_dst:
    if dist not in [None, 0, 30, 60, 90]:
      raise Exception('dist_org/dist_dst must be in [0,30,60,90]')
  wd.get('http://www.aa.com/reservation/oneWaySearchAccess.do')
  wd.getid('flightSearchForm.originAirport').clear().send_keys(org)
  wd.getid('flightSearchForm.destinationAirport').clear().send_keys(dst)
  wd.getid('flightSearchForm.originAlternateAirportDistance').option(dist_org)
  wd.getid('flightSearchForm.destinationAlternateAirportDistance').option(dist_dst)
  wd.getid('flightSearchForm.searchType.matrix').click()
  wd.getid('flightSearchForm.flightParams.flightDateParams.travelMonth').option(date.month)
  wd.getid('flightSearchForm.flightParams.flightDateParams.travelDay').option(date.day)
  wd.getid('flightSearchForm.flightParams.flightDateParams.searchTime').option(120001)
  wd.getid('flightSearchForm.carrierAll').click()
  wd.ckpt()
  wd.getid('flightSearchForm').submit()
  def gen():
    for x in wd.csss('.tabNotActive, .highlightSubHeader'):
      date, prc = x.text.split('from')
      yield toprc(prc), parse_date(date)
  return list(gen())

@retry_if_timeout
def virginamerica(wd, org, dst, date):
  """
  Note that this airline has very limited airport options.

  Returns list of (best price, day) pairs for +/- 3 days around date.
  """
  wd.get('http://virginamerica.com')
  wd.getid('owRadio').click()
  wd.xpath('//select[@name="flightSearch.origin"]/option[@value=%r]' % org.upper()).click()
  wd.xpath('//select[@name="flightSearch.destination"]/option[@value=%r]' % dst.upper()).click()
  wd.name('flightSearch.depDate.MMDDYYYY').clear().send_keys(fmt_date(date)).tab().delay()
  wd.ckpt()
  wd.getid('SearchFlightBt').click()
  return [(toprc(prc), parse_date(day.text))
      for prc, day in zip(wd.xpaths('//*[@class="fsCarouselCost"]'),
                          wd.xpaths('//*[@class="fsCarouselDate"]'))]

@retry_if_timeout
def bing(wd, org, dst, date, near_org=False, near_dst=False):
  """
  Returns [(best price, date)].
  """
  wd.get('http://bing.com/travel')
  wd.getid('oneWayLabel').click()
  wd.getid('orig1Text').click().clear().send_keys(org).tab()
  wd.getid('dest1Text').click().clear().send_keys(dst).tab()
  if near_org: wd.getid('no1').wait_displayed().set(True)
  if near_dst: wd.getid('ne1').wait_displayed().set(True)
  wd.getid('leave1').clear().send_keys(fmt_date(date))
  wd.getid('PRI-HP').set(False)
  wd.ckpt()
  wd.find_element_by_css_selector('.sbmtBtn').click()
  # Wait for "still searching" to disappear.
  while wd.getid('searching').is_displayed(): time.sleep(1)
  return [(toprc(wd.xpath('//span[@class="price"]')), date)]

@retry_if_timeout
def southwest(wd, org, dst, date):
  """
  Returns list of (best price, date) pairs for month around date.
  """
  wd.get('http://www.southwest.com/cgi-bin/lowFareFinderEntry')
  wd.getid('oneWay').click()
  wd.getid('originAirport_displayed').clear().send_keys(org).tab()
  wd.getid('destinationAirport_displayed').clear().send_keys(dst).tab()
  wd.getid('outboundDate').option(fmt_date(month_of(date)))
  wd.ckpt()
  wd.getid('submitButton').click()
  month = wd.css('.carouselTodaySodaIneligible .carouselBody').text
  def gen():
    for x in wd.csss('.fareAvailableDay'):
      day, prc = x.text.split('\n')
      yield toprc(prc), parse_date('%s %s' % (month, day))
  return list(gen())

@retry_if_timeout
def delta(wd, org, dst, date, nearby=False):
  """
  Returns [(best price, date)].
  """
  wd.get('http://www.delta.com/booking/searchFlights.do')
  wd.getid('oneway_link').click()
  wd.getid('departureCity_0').clear().send_keys(org)
  wd.getid('destinationCity_0').clear().send_keys(dst)
  if nearby: wd.getid('flexAirports').set(True)
  wd.getid('departureDate_0').clear().send_keys(fmt_date(date))
  wd.ckpt()
  wd.getid('Go').click()
  return [(toprc(wd.css('.lowest .fares').text), date)]

@retry_if_timeout
def farecmp():
  pass

@retry_if_timeout
def jetblue():
  pass

@contextlib.contextmanager
def quitting(x):
  try: yield x
  finally: x.quit()

@contextlib.contextmanager
def subproc(*args, **kwargs):
  p = subprocess.Popen(*args, **kwargs)
  try: yield p
  finally: p.terminate(); p.wait()

def script(wd, cfg):
  now = dt.datetime.now()
  org, dst, date = 'sfo', 'phl', dt.date(2012,12,21)
  cal = calendar.Calendar(6)

  html_path = 'report %s.html' % (now,)
  def pre_path(label): return '%s presubmit (%s).png' % (label, now)
  def post_path(label): return '%s postsubmit (%s).png' % (label, now)
  def wrap(label, func):
    class very_rich_driver(rich_driver):
      def ckpt(self):
        self.wd.save_screenshot(pre_path(label))
    try: return func(very_rich_driver(wd, cfg.debug))
    finally: wd.save_screenshot(post_path(label))

  def gen():
    yield 'southwest sfo to phl', 'southwest sfo to phl', [(249, date)]
    yield 'southwest sjc to phl', 'southwest sjc to phl', [(229, date)]
    yield 'united', 'united', [(229, date+rd.relativedelta(days=0)),
                               (229, date+rd.relativedelta(days=1))]
    return
    yield 'united', 'united', wrap(
        lambda wd: united(wd, org, dst, date, nearby=True))
    yield 'aa', 'aa', wrap(
        lambda wd: aa(wd, org, dst, date, dist_org=60, dist_dst=30))
    yield 'virginamerica', 'virginamerica', wrap(
        lambda wd: virginamerica(wd, org, dst, date))
    for offset in xrange(-3, 4, 1):
      dat = date + rd.relativedelta(days=offset)
      yield 'bing', 'bing %s' % dat, wrap(
          lambda wd: bing(wd, org, dst, dat, near_org=True, near_dst=True))
    yield 'southwest sfo to phl', 'southwest sfo to phl', wrap(
        lambda wd: southwest(wd, org, dst, date))
    yield 'southwest sjc to phl', 'southwest sjc to phl', wrap(
        lambda wd: southwest(wd, 'sjc', dst, date))
    yield 'southwest oak to phl', 'southwest oak to phl', wrap(
        lambda wd: southwest(wd, 'oak', dst, date))
    for offset in xrange(-3, 4, 1):
      dat = date + rd.relativedelta(days=offset)
      yield 'delta', 'delta %s' % dat, wrap(
          lambda wd: delta(wd, org, dst, dat, nearby=True))

  # combine by date
  resinfo = collections.namedtuple('resinfo', 'prc group label')
  date2res = {}
  for group, label, res in gen():
    for prc,dat in res:
      date2res.setdefault(dat, []).append(resinfo(prc, group, label))
  ngroups = len(set(r.group for res in date2res.values() for r in res))

  # text/email report
  def gen_vals():
    for dow in cal.iterweekdays():
      yield '%6s' % calendar.day_abbr[dow]
    yield '\n'
    for day, dow in cal.itermonthdays2(*date.timetuple()[:2]):
      dat = date + rd.relativedelta(day=day)
      res = date2res.get(dat, [])
      val = '$%s' % min(r.prc for r in res) \
            if day > 0 and len(res) == ngroups else ''
      yield '%6s%s' % (val, '\n' if dow == 5 else '')
  def gen_days():
    for day, dow in cal.itermonthdays2(*date.timetuple()[:2]):
      yield '%6s%s' % ('' if day == 0 else day, '\n' if dow == 5 else '')
  vals = ''.join(gen_vals()).split('\n')
  days = ''.join(gen_days()).split('\n')
  text_report = '\n'.join(line for lines in zip(vals, days) for line in lines)
  text_report = '''
%s

<https://yz.mit.edu/flights/%s>
'''.strip() % (text_report, urllib.quote_plus(html_path))
  print text_report

  # web report
  rows = []
  for day,dow in cal.itermonthdays2(*date.timetuple()[:2]):
    if dow == 6: rows.append([])
    rows[-1].append((day,dow))
  labels = sorted(set(r.label for res in date2res.itervalues() for r in res))
  tmpl = '''
!!! 5
html(lang='en')
  head
    title Flight Scraper Results for #{now}
    link(href='//netdna.bootstrapcdn.com/twitter-bootstrap/2.1.1/css/bootstrap-combined.min.css', rel='stylesheet')
    link(href='main.css', rel='stylesheet')
  body
    h1 Flight Scraper Results for #{now}
    table.table.table-bordered
      thead
        tr
          for dow in cal.iterweekdays()
            th= calendar.day_abbr[dow]
      tbody
        for row in rows
          tr
            for day, dow in row
              td
                if day > 0
                  .day-number= day
                  - dat = date + rd.relativedelta(day=day)
                  - res = date2res.get(dat, [])
                  - best = "$%s" % min(r.prc for r in res) if res else '-'
                  if len(res) == ngroups
                    .full.price= best
                  else
                    .partial.price= best
    table.table.table-striped.table-hover
      col
      col
      col
      col(style='text-align: right')
      thead
        tr
          th Date
          th Search
          th Price
      tbody
        for date, res in sorted(date2res.items())
          for r in res
            tr
              td= date
              td
                a(href="#label-#{labels.index(r.label)}")= r.label
              td $#{r.prc}
    .screenshots
      for i, label in enumerate(labels)
        a(name="label-#{i}")
        h2= label
        h3 Pre-submit
        img(src="#{pre_path(label)}")
        h3 Post-submit
        img(src="#{post_path(label)}")
    script(src='//ajax.googleapis.com/ajax/libs/jquery/1.8.2/jquery.min.js')
    script(src='//netdna.bootstrapcdn.com/twitter-bootstrap/2.1.1/js/bootstrap.min.js')
    script(src='main.js')
  '''
  compiler = html_compiler(pyjade.Parser(tmpl).parse())
  env = dict(globals())
  env.update(locals())
  with pyjade.ext.html.local_context_manager(compiler, env):
    html = compiler.compile()
  with open(html_path, 'w') as f:
    f.write(html)

  return text_report

def main(argv = sys.argv):
  default_from = '%s@%s' % (getpass.getuser(), socket.getfqdn())

  p = argparse.ArgumentParser(description=__doc__)
  p.add_argument('-d', '--debug', action='store_true',
      help='Run browser directly, without Xvfb.')
  p.add_argument('-T', '--mailto',
      help='''Email addresses where results should be sent. Without this, just
      print results to stdout.''')
  p.add_argument('-F', '--mailfrom', default=default_from,
      help='Email address results are sent from. (default: %s)' % default_from)
  cfg = p.parse_args(argv[1:])

  cmd = 'sleep 99999999' if cfg.debug else 'Xvfb :10 -screen 0 1600x1200x24'
  with subproc(cmd.split()) as xvfb:
    if not cfg.debug: os.environ['DISPLAY'] = ':10'
    # This silencing isn't working
    stdout, stderr = sys.stdout, sys.stderr
    sys.stdout = open('/dev/null','w')
    sys.stderr = open('/dev/null','w')
    with quitting(webdriver.Chrome()) as wd:
      sys.stdout, sys.stderr = stdout, stderr
      out = script(wd, cfg)

  if cfg.mailto:
    mail = MIMEText(out)
    mail['From'] = cfg.mailfrom
    mail['To'] = cfg.mailto
    mail['Subject'] = 'Flight alert for %s' % \
        (dt.datetime.now().strftime('%a %Y-%m-%d %I:%M %p'),)
    with contextlib.closing(smtplib.SMTP('localhost')) as smtp:
      smtp.sendmail(mail['From'], mail['To'].split(','), mail.as_string())
