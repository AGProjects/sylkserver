# Copyright (C) 2012 AG Projects. See LICENSE for details
#

from cStringIO import StringIO
from formatter import AbstractFormatter, DumbWriter
from htmllib import HTMLParser, HTMLParseError

__all__ = ['html2text', 'text2html', 'format_uri']


def html2text(data):
    # Based on http://stackoverflow.com/questions/328356/extracting-text-from-html-file-using-python
    f = StringIO()
    parser = HTMLParser(AbstractFormatter(DumbWriter(f)))
    try:
        parser.feed(data)
    except HTMLParseError:
        return ''
    else:
        parser.close()
        return f.getvalue()


xhtml_im_template = """<html xmlns='http://jabber.org/protocol/xhtml-im'>
    <body xmlns='http://www.w3.org/1999/xhtml'>
    %(data)s
    </body>
</html>"""

def text2html(data):
    return xhtml_im_template % {'data': data}


def format_uri(uri, scheme=''):
    return '%s%s@%s' % ('' if not scheme else scheme+':', uri.user, uri.host)

