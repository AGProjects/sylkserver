
import lxml.html
import lxml.html.clean

__all__ = ['html2text', 'text2html', 'format_uri']


def html2text(data):
    try:
        doc = lxml.html.document_fromstring(data)
        cleaner = lxml.html.clean.Cleaner(style=True)
        doc = cleaner.clean_html(doc)
        return doc.text_content().strip('\n')
    except Exception:
        return ''


xhtml_im_template = """<html xmlns='http://jabber.org/protocol/xhtml-im'>
    <body xmlns='http://www.w3.org/1999/xhtml'>
    %(data)s
    </body>
</html>"""

def text2html(data):
    return xhtml_im_template % {'data': data}


def format_uri(uri, scheme=''):
    return '%s%s@%s' % ('' if not scheme else scheme+':', uri.user, uri.host)

