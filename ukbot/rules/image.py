# encoding=utf-8
# vim: fenc=utf-8 et sw=4 ts=4 sts=4 ai
import re
from mwclient.errors import InvalidPageTitle
import urllib
import logging
from ..common import _
from ..contributions import UserContribution
from .rule import Rule
from .decorators import family

logger = logging.getLogger(__name__)


class ImageRule(Rule):

    rule_name = 'image'

    def __init__(self, sites, template, trans=None):
        Rule.__init__(self, sites, template, trans)

        # Points for added images that are also uploaded by the same user, but not marked as 'own work'.
        self.points_own = self.get_param('own', datatype=float)

        # Points for added images that are also uploaded by the same user, and also marked as 'own work'.
        self.points_ownwork = self.get_param('ownwork', datatype=float)

        # Max number of images already present in the article. Set this to 0 to only give points
        # for addition of images to articles that do not have images from before.
        self.maxinitialcount = self.get_param('maxinitialcount', datatype=int)

        # Find localized File: prefixes for all sites
        self.file_prefixes = set([prefix for site in self.sites.sites.values() for prefix in site.file_prefixes])

        # Keep a statistic for total number of images added
        self.total = 0

        # Compile regexps for match images
        prefixes = r'(?:%s)' % '|'.join(['%s:' % x for x in self.file_prefixes])
        suffixes = r'\.(?:svg|png|jpe?g|gif|tiff|webp|xcf)'
        imagematcher = r"""
            (?:
                (?:=|\||^)%(prefixes)s?   # "=File:", "=", "|File:", "|", ...
                | %(prefixes)s
            )
            (  # start capture
                [^\}\]\[=\|$\n]*?
                %(suffixes)s
            )  # end capture
        """ % {'prefixes': prefixes, 'suffixes': suffixes}
        logger.debug('ImageRule regexp: %s', imagematcher)
        self.imagematcher = re.compile(imagematcher, flags=re.IGNORECASE | re.MULTILINE | re.VERBOSE)
        self.extlinkmatcher = re.compile(r'https?://[^ \n]*?' + suffixes, flags=re.IGNORECASE)

    def get_images(self, txt):
        txt = self.extlinkmatcher.sub('', txt)  # remove external links to images
        # return len(re.findall(imagematcher, txt, flags=re.IGNORECASE))
        for img in self.imagematcher.finditer(txt):
            yield img.group(1).strip()

    @staticmethod
    def get_image_uploader(rev, filename):
        try:
            image = rev.article().site().images[filename]
        except InvalidPageTitle:
            logger.error('Image filename "%s" is invalid, ignoring this file', filename)
            return
        imageinfo = image.imageinfo
        if len(imageinfo) > 0:  # seems like image.exists only checks locally
            try:
                uploader = imageinfo['user']
            except KeyError:
                logger.error("Could not locate user for file '%s' in rev. %s ",
                             filename, rev.revid)
                return

            logger.debug("File '%s' uploaded by '%s', revision made by '%s'",
                         filename, uploader, rev.username)
            return uploader

    @staticmethod
    def get_image_credit(rev, filename):
        credit = ''
        extrainfo = rev.article().site().api('query', prop='imageinfo', titles=u'File:{}'.format(filename),
                                             iiprop='extmetadata')
        try:
            for pageid, page in extrainfo['query']['pages'].items():
                credit = page['imageinfo'][0]['extmetadata']['Credit']['value']
        except KeyError:
            logger.debug("Could not read credit info for file '%s'", filename)

        return credit

    @family('wikipedia.org', 'wikibooks.org')
    def test(self, rev):
        imgs_before = list(self.get_images(rev.parenttext))
        imgs_after = list(self.get_images(rev.text))
        added = set(imgs_after).difference(set(imgs_before))

        if len(added) == 0:
            return

        counters = {'ownwork': [], 'own': [], 'other': []}
        for filename in added:
            filename = urllib.parse.unquote(filename)
            uploader = self.get_image_uploader(rev, filename)
            if uploader is None:
                logger.warning("File '%s' does not exist or is invalid", filename)
                continue

            if uploader == rev.username:
                credit = self.get_image_credit(rev, filename)

                if 'int-own-work' in credit or 'Itse otettu valokuva' in credit:
                    logger.debug("File '%s' identified as own work.", filename)
                    counters['ownwork'].append(filename)
                else:
                    logger.debug("File '%s' identified as own upload, but not own work.", filename)
                    counters['own'].append(filename)
            else:
                logger.debug("File '%s' not identified as own upload or own work.", filename)
                counters['other'].append(filename)

        # Update statistic
        self.total += len(counters['own']) + len(counters['ownwork']) + len(counters['other'])

        # If maxinitialcount is 0, only the first image counts.
        # If an user adds both an own image and an image by someone else,
        # we should make sure to credit the own image, not the other.
        # We therefore process the own images first.
        points = 0
        for n, img in enumerate(added):
            if self.maxinitialcount is None or len(imgs_before) + n <= self.maxinitialcount:
                if img in counters['ownwork']:
                    points += self.points_ownwork or self.points_own or self.points
                elif img in counters['own']:
                    points += self.points_own or self.points
                else:
                    points += self.points

        yield UserContribution(rev=rev, rule=self, points=points,
                               description=_('images') % {'images': len(added)})
