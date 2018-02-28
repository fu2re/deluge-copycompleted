#
# core.py
#
# Copyright (C) 2009 fu2re <fu2re@yandex.ru>
#
# Basic plugin template created by:
# Copyright (C) 2008 Martijn Voncken <mvoncken@gmail.com>
# Copyright (C) 2007-2009 Andrew Resch <andrewresch@gmail.com>
# Copyright (C) 2009 Damien Churchill <damoxc@gmail.com>
#
# Deluge is free software.
#
# You may redistribute it and/or modify it under the terms of the
# GNU General Public License, as published by the Free Software
# Foundation; either version 3 of the License, or (at your option)
# any later version.
#
# deluge is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with deluge.    If not, write to:
#     The Free Software Foundation, Inc.,
#     51 Franklin Street, Fifth Floor
#     Boston, MA  02110-1301, USA.
#
#    In addition, as a special exception, the copyright holders give
#    permission to link the code of portions of this program with the OpenSSL
#    library.
#    You must obey the GNU General Public License in all respects for all of
#    the code used other than OpenSSL. If you modify file(s) with this
#    exception, you may extend this exception to your version of the file(s),
#    but you are not obligated to do so. If you do not wish to do so, delete
#    this exception statement from your version. If you delete this exception
#    statement from all source files in the program, then also delete it here.
#
import re
import os
import shutil
import thread
from deluge.log import LOG as log
from deluge.plugins.pluginbase import CorePluginBase
import deluge.component as component
import deluge.configmanager
from deluge.core.rpcserver import export
from deluge.event import DelugeEvent
from twisted.python.filepath import FilePath
from twisted.internet import reactor

TEST_VIDEO = re.compile('.*(' + '|'.join(['mkv', 'mp4']) + ')$')
TEST_SUB1 = re.compile('.*(' + '|'.join(['ass', 'ssa']) + ')$')
TEST_SUB2 = re.compile('.*(srt)$')


class TorrentCopiedEvent(DelugeEvent):
    """
    Emitted when a torrent is copied.
    """

    def __init__(self, torrent_id, old_path, new_path, path_pairs):
        """
        :param torrent_id - hash representing torrent in Deluge
        :param old_path - original path for the torrent
        :param new_path - new path for the torrent
        :param path_pairs - a list of tuples, ( old path, new path )
        """
        self._args = [torrent_id, old_path, new_path, path_pairs]


class Core(CorePluginBase):
    def enable(self):
        self.config = deluge.configmanager.ConfigManager("copysubtitles.conf", {
            'lang': 'ru|rus|russian'
        })
        # Get notified when a torrent finishes downloading
        component.get("EventManager").register_event_handler("TorrentFinishedEvent", self.on_torrent_finished)

    def disable(self):
        try:
            self.timer.cancel()
        except:
            pass
        component.get("EventManager").deregister_event_handler("TorrentFinishedEvent", self.on_torrent_finished)

    def update(self):
        pass

    @staticmethod
    def score_subtitles_folder(lang, count, location):
        files = os.listdir(location)
        s1 = filter(TEST_SUB1.match, files)
        s2 = filter(TEST_SUB2.match, files)
        f1 = len(s1)
        f2 = len(s2)
        return - (
            # choose folder with own language
            int(bool(re.search('(' + lang + ')+', location.lower()))) * 1000000 +
            # choose folder with full set of subtitles
            int((f1 + f2) >= count) * 100000 +
            # prefer ass/ssa
            f1 * 2 +
            # or at least srt
            f2
        ), set(s1) | set(s2)

    @staticmethod
    def get_contents(location, test=None, method='walk'):
        pathObj = FilePath(location)
        for subpath in getattr(pathObj, method)():
            if not test or test(subpath.path):
                yield subpath.path

    @staticmethod
    def get_sub_folders(location):
        return Core.get_contents(location, test=lambda x: os.path.isdir(x))

    @staticmethod
    def get_root_folder(location):
        l2 = os.path.dirname(location)
        if not l2:
            return location
        return Core.get_root_folder(l2)

    @staticmethod
    def get_video_folders(location, files):
        root_folders = set([Core.get_root_folder(f.path) for f in files])
        for rf in root_folders:
            if not rf:
                continue
            loc = os.path.join(location, rf)
            for d in set([
                os.path.dirname(path) for path in Core.get_contents(loc, test=lambda x: TEST_VIDEO.match(x))
            ]):
                yield d

    def find_subtitles(self, location):
        files = os.listdir(location)
        episodes_count = len(filter(TEST_VIDEO.match, files))
        subtitle_count = len(filter(TEST_SUB1.match, files)) + len(filter(TEST_SUB2.match, files))
        if subtitle_count >= episodes_count:
            # subtitles already here
            return

        folders = Core.get_sub_folders(location)
        for entry in folders:
            score, files = Core.score_subtitles_folder(self.config["lang"], episodes_count, entry)
            if not files:
                continue
            yield score, entry, files

    def find_video(self, torrent_id, video_folders):
        try:
            video_folder = next(video_folders)
            self.find_subtitles(video_folder)
            subtitle_folders = sorted(list(self.find_subtitles(video_folder)))

            if subtitle_folders:
                _score, subtitle_folder, files = subtitle_folders[0]
                thread.start_new_thread(
                    Core._thread_copy, (torrent_id, video_folder, subtitle_folder, files)
                )
            self.find_video(torrent_id, video_folders)
        except StopIteration:
            return

    def on_torrent_finished(self, torrent_id):
        """
        Copy the torrent now. It will do this in a separate thread to avoid
        freezing up this thread (which causes freezes in the daemon and hence
        web/gtk UI.)
        """
        torrent = component.get("TorrentManager").torrents[torrent_id]
        info = torrent.get_status(["name", "save_path", "move_on_completed", "move_on_completed_path"])
        location = info["move_on_completed_path"] if info["move_on_completed"] else info["save_path"]
        self.find_video(torrent_id, Core.get_video_folders(location, torrent.get_files()))

    @staticmethod
    def _thread_copy(torrent_id, video_folder, subtitle_folder, files):
        path_pairs = []
        for filename in files:
            try:
                old_file_path = os.path.join(subtitle_folder, filename)
                new_file_path = os.path.join(video_folder, filename)

                # check that this file exists at the current location
                # if not os.path.exists(old_file_path):
                #     log.debug("COPYSUBTITLES: %s was not downloaded. Skipping." % f["path"])
                #     break

                # check that this file doesn't already exist at the new location
                if os.path.exists(new_file_path):
                    log.info("COPYSUBTITLES: %s already exists in the destination. Skipping." % f["path"])
                    break

                log.info("COPYSUBTITLES: Copying %s to %s" % (old_file_path, new_file_path))

                # ensure dirs up to this exist
                if not os.path.exists(os.path.dirname(new_file_path)):
                    os.makedirs(os.path.dirname(new_file_path))

                # copy the file
                shutil.copy2(old_file_path, new_file_path)
                path_pairs.append((old_file_path, new_file_path))

            except Exception, e:
                os.error("COPYSUBTITLES: Could not copy file.\n%s" % str(e))

        component.get("EventManager").emit(TorrentCopiedEvent(torrent_id, subtitle_folder, video_folder, path_pairs))

    @export()
    def set_config(self, config):
        "sets the config dictionary"
        for key in config.keys():
            self.config[key] = config[key]
        self.config.save()

    @export()
    def get_config(self):
        "returns the config dictionary"
        return self.config.config
