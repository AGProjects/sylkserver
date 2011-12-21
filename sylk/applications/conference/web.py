# Copyright (C) 2011 AG Projects. See LICENSE for details
#

__all__ = ['ScreenSharingWebServer']

import os
import urllib

from twisted.web import server, static, resource
from twisted.internet import reactor


html_template = """
<html>                                                                                                                       
<head>
    <META HTTP-EQUIV=Refresh CONTENT="10">
    <title>SylkServer Screen Sharing</title>
</head>

<script type="text/javascript">
    var today = new Date();
    var c = today.getTime();
        
    function reloadScreen()
    {
        document.screenImage.src = "%(image)s" + "?" + c;
        c = c + 1;
    }
        
    function startTimer()
    {
        setTimeout('reloadScreen()', 1000);
    }
</script>

<body bgcolor="#999999">
    <div>
        <img src='%(image)s' name='screenImage' onload='startTimer()' style='position: relative; top: 0px; margin: 0px 0px 0px 0px; clear: both; float: left; %(width)s' />
    </div>
</body>

</html>                    

"""


class ScreenSharingWebsite(resource.Resource):
    isLeaf = True

    def __init__(self, path):
        self.base_path = path
        resource.Resource.__init__(self)

    def render_GET(self, request):
        if 'image' not in request.args or not request.args.get('image', [''])[0].endswith('jpg'):
            return "<html><body>Screenshot image not provided</body></html>"
        image_path = urllib.unquote(request.args['image'][0])
        if not os.path.isfile(os.path.join(self.base_path, image_path)):
            return "<html><body>Screenshot image is not readable</body></html>"
        image = os.path.join('/img', image_path)
        width = 'width: 100%' if 'fit' in request.args else ''
        return html_template % dict(image=image, width=width)


class ScreenSharingWebServer(object):

    def __init__(self, images_path):
        root = resource.Resource()
        home = ScreenSharingWebsite(images_path)
        img_resource = static.File(images_path)
        root.putChild('', home)
        root.putChild('img', img_resource)

        self.site = server.Site(root)
        self.listener = None

    @property
    def port(self):
        if self.listener is None:
            return 0
        return self.listener.getHost().port

    def run(self, interface, port):
        self.listener = reactor.listenTCP(port, self.site, interface=interface)

