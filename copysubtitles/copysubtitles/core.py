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
from langdetect import detect_langs
from langdetect.lang_detect_exception import LangDetectException
import pysubs2


TEST_VIDEO = re.compile('.*(' + '|'.join(['mkv', 'mp4', 'avi', 'mpg']) + ')$')
TEST_SUB1 = re.compile('.*(' + '|'.join(['ass', 'ssa']) + ')$')
TEST_SUB2 = re.compile('.*(srt)$')
# default density is 243 events for 23 min
DENS = 243 / 1418930.
ACCURACY = .65


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
        """

        :return:
        """
        self.config = deluge.configmanager.ConfigManager("copysubtitles.conf", {
            'lang': 'ru|rus'
        })
        # Get notified when a torrent finishes downloading
        component.get("EventManager").register_event_handler("TorrentFinishedEvent", self.on_torrent_finished)

    def disable(self):
        """

        :return:
        """
        try:
            self.timer.cancel()
        except:
            pass
        component.get("EventManager").deregister_event_handler("TorrentFinishedEvent", self.on_torrent_finished)

    def update(self):
        pass

    @staticmethod
    def get_lang_prob(lang, text):
        """
        :param lang: desired language
        :param text: contested text
        :type lang: str
        :type text: str
        :return: score for the matched language
        """
        try:
            for l in filter(lambda p: p.lang == lang, detect_langs(text)):
                return l.prob
            return 0
        except LangDetectException:
            return 0

    @staticmethod
    def score_subtitles_folder(languages, count, location):
        """
        get usability score for selected location and list of subtitle
        file names near to their language.
        Language is defined by simple majority vote. For example if 2 of 3
        contested files is defined as RU - all the files will be marked as RU

        :param languages: part of language regexp
        :param count: count of subtitle files we are looking for
        :param location: contested location
        :type languages: str
        :type count: int
        :type location: str
        :return: score (lower is better) and list of tuples. E.g. -132211, [('a.ass', 'ru'), ('b.ass', 'ru')]
        """
        lang = languages.split('|')[0]
        score = 0
        density = 0
        files = os.listdir(location)
        s1 = filter(TEST_SUB1.match, files)
        s2 = filter(TEST_SUB2.match, files)
        subs = list(set(s1) | set(s2))
        subs_lang = []
        fs = len(subs) or 10 ** -5
        f1 = len(s1)
        f2 = len(s2)
        # number of contested lines. Lower is better for performance.
        width = 30
        sl = float(min(3, fs))
        # contest some files
        for filename in subs[:int(sl)]:
            # check existed suffix. it will be equal to 0 if it does not exist
            f_score = int(bool(re.search('\.(' + languages + ')+\.', filename.lower())))
            # load subtitles and check it length
            path = os.path.join(location, filename)
            sub = pysubs2.load(path)
            coverage = len(sub)
            f_density = (coverage / float(sub[-1].end)) / DENS
            # if language score is still 0 check it more closely
            if not f_score:
                # we should not start from begging in case of intro
                # that's why we try to get part from a middle
                start = max((coverage/2) - (width/2), 0)
                # check language for the selected part
                for line in sub[start:(start+width)]:
                    f_score += Core.get_lang_prob(lang, line.text)
            # normalize the score
            f_score = f_score / float(min(coverage, width))
            # append language to majority vote list if it accurate enough
            subs_lang.append(lang if f_score > ACCURACY else None)
            # stack language score and density
            score += f_score
            density += f_density
        # get the final scores
        lng_score = int((score / sl) > ACCURACY)
        cnt_score = int(fs >= count)
        dns_score = min(round(density / sl), 1)
        ssa_score = round(f1 / float(fs), 2)
        srt_score = round(f2 / float(fs), 2)
        majority = len(set(subs_lang)) == 1
        log.info("COPYSUBTITLES: scores for %s - %s, %s, %s, %s, %s" % \
                 (location, lng_score, cnt_score, dns_score, ssa_score, srt_score))
        return -(
            lng_score * 10 ** 5 +
            cnt_score * 10 ** 4 +
            dns_score * 10 ** 3 +
            ssa_score * 10 ** 2 +
            srt_score
        ), zip(subs, [lang if majority else None] * int(len(subs)))

    @staticmethod
    def get_contents(location, test=None, method='walk'):
        """
        Get folder contents
        :param location: contested location
        :param test: filter function
        :param method: one of available twisted.FilePath methods.
        Should be on of childrens / walk
        :return: matched paths
        :rtype: generator
        """
        path_obj = FilePath(location)
        for sub_path in getattr(path_obj, method)():
            if not test or test(sub_path.path):
                yield sub_path.path

    @staticmethod
    def get_sub_folders(location):
        """
        get all sub folders recursievly
        :param location: contested location
        :return: matched paths
        :rtype: generator
        """
        return Core.get_contents(location, test=lambda x: os.path.isdir(x))

    @staticmethod
    def get_root_folder(location):
        """
        Get root folder for the given path
        :param location: contested location
        :return: matched path
        :rtype: str
        """
        l2 = os.path.dirname(location)
        if not l2:
            return location
        return Core.get_root_folder(l2)

    @staticmethod
    def get_video_folders(location, files):
        """
        Get sub folders which contains any video files
        :param location: contested location
        :param files: list of torrent files
        :return: matched paths
        :rtype: generator
        """
        root_folders = set([Core.get_root_folder(f['path']) for f in files])
        for rf in root_folders:
            if not rf:
                continue
            loc = os.path.join(location, rf)
            for d in set([
                os.path.dirname(path) for path in Core.get_contents(loc, test=lambda x: TEST_VIDEO.match(x))
            ]):
                yield d

    def find_subtitles(self, location):
        """

        :param location: contested location
        :return:
        :rtype: generator
        """
        all_files = os.listdir(location)
        episodes_count = len(filter(TEST_VIDEO.match, all_files))
        subtitle_files = filter(TEST_SUB1.match, all_files) + filter(TEST_SUB2.match, all_files)
        # if subtitles already here check suffixes only
        log.info("COPYSUBTITLES: %s of %s already presented" % (subtitle_files, episodes_count))
        if len(subtitle_files) >= episodes_count:
            yield -10 ** 10, location, subtitle_files

        else:

            folders = Core.get_sub_folders(location)
            for entry in folders:
                score, files = Core.score_subtitles_folder(
                    self.config["lang"], episodes_count, entry
                )
                if not files:
                    continue
                yield score, entry, files

    def on_torrent_finished(self, torrent_id):
        """
        Copy the torrent now. It will do this in a separate thread to avoid
        freezing up this thread (which causes freezes in the daemon and hence
        web/gtk UI.)
        :param torrent_id:
        :type torrent_id: int
        :return:
        """
        torrent = component.get("TorrentManager").torrents[torrent_id]
        info = torrent.get_status(["name", "save_path", "move_on_completed", "move_on_completed_path"])

        # get the destination path
        location = info["move_on_completed_path"] if info["move_on_completed"] else info["save_path"]

        # we assume about any subtitle for should be forced
        _p, rest = os.path.split(location)
        forced = rest.lower() == 'anime'

        # lets do the job
        video_folders = Core.get_video_folders(location, torrent.get_files())
        for video_folder in video_folders:
            # sort subtitle folders according to their score
            subtitle_folders = sorted(list(self.find_subtitles(video_folder)))

            if not subtitle_folders:
                continue

            _score, subtitle_folder, files = subtitle_folders[0]
            log.info("COPYSUBTITLES: Matched %s with score %s" % (subtitle_folder, _score))
            thread.start_new_thread(
                Core._thread_copy, (torrent_id, video_folder, subtitle_folder, files, forced)
            )

    @staticmethod
    def _thread_copy(torrent_id, video_folder, subtitle_folder, files, forced):
        """
        copy files

        :param torrent_id:
        :param video_folder: destination folder
        :param subtitle_folder: source folder
        :param files: list of source files combined with language ('a.ass', 'ru')
        :param forced: append forced suffix
        :type torrent_id: int
        :type video_folder: str
        :type subtitle_folder: str
        :type files: list
        :type forced: boolean
        :return:
        """
        path_pairs = []
        for filename, lang in files:
            try:
                old_file_path = os.path.join(subtitle_folder, filename)
                filename, file_extension = os.path.splitext(filename)
                suffixes = filename.lower().split('.')

                if lang and lang not in suffixes:
                    filename += '.' + lang
                if forced and 'forced' not in suffixes:
                    filename += '.forced'

                new_file_path = os.path.join(video_folder, ''.join((filename, file_extension)))

                # check that this file exists at the current location
                # if not os.path.exists(old_file_path):
                #     log.debug("COPYSUBTITLES: %s was not downloaded. Skipping." % f["path"])
                #     break

                # check that this file doesn't already exist at the new location
                if os.path.exists(new_file_path):
                    log.info("COPYSUBTITLES: %s already exists in the destination. Skipping." % new_file_path)
                    continue

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
        """
        sets the config dictionary
        :param config:
        :return:
        """
        for key in config.keys():
            self.config[key] = config[key]
        self.config.save()

    @export()
    def get_config(self):
        """
        returns the config dictionary
        :return:
        """
        return self.config.config
