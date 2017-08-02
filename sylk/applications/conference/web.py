
import os
import urllib

from application.python.types import Singleton
from twisted.web.resource import ErrorPage, NoResource
from twisted.web.static import File

from sylk.applications.conference.configuration import ConferenceConfig
from sylk.web import Klein


class ConferenceWeb(object):
    __metaclass__ = Singleton

    app = Klein()

    screensharing_template = """
        <html>
        <head>
            <title>SylkServer Screen Sharing</title>
        </head>
        <script type="text/javascript">
            var today = new Date();
            var c = today.getTime();

            function reloadScreen() {
                document.screenImage.src = "%(image)s" + "?" + c;
                c = c + 1;
            }

            function startTimer() {
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

    def __init__(self, conference):
        self._resource = self.app.resource()
        self.conference = conference

    @property
    def resource(self):
        return self._resource

    @app.route('/')
    def home(self, request):
        return NoResource('Nothing to see here, move along.')

    @app.route('/<string:room_uri>')
    def room(self, request, room_uri):
        return NoResource('Nothing to see here, move along.')

    @app.route('/<string:room_uri>/screensharing')
    def scheensharing(self, request, room_uri):
        try:
            room = self.conference._rooms[room_uri]
        except KeyError:
            return NoResource('Room not found')
        request.setHeader('Content-Type', 'text/html; charset=utf-8')
        if 'image' not in request.args or not request.args.get('image', [''])[0].endswith('jpg'):
            return ErrorPage(400, 'Bad Request', '\"image\" not provided')
        images_path = os.path.join(ConferenceConfig.screensharing_images_dir, room.uri)
        image_path = os.path.basename(urllib.unquote(request.args['image'][0]))
        if not os.path.isfile(os.path.join(images_path, image_path)):
            return NoResource('Image not found')
        image = os.path.join('screensharing_img', image_path)
        width = 'width: 100%' if 'fit' in request.args else ''
        return self.screensharing_template % dict(image=image, width=width)

    @app.route('/<string:room_uri>/screensharing_img/<string:filename>')
    def screensharing_image(self, request, room_uri, filename):
        try:
            room = self.conference._rooms[room_uri]
        except KeyError:
            return NoResource('Room not found')
        images_path = os.path.join(ConferenceConfig.screensharing_images_dir, room.uri)
        return File(os.path.join(images_path, os.path.basename(filename)))

