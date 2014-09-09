#!/usr/bin/env python2.7
import os
import re
import sys
import glob
import pytz
import sass
import time
import shutil
import urllib2
import datetime
import markdown
import StringIO
import subprocess
import lxml.etree
import easywebdav
import ConfigParser
import htmlmin.minify

class DAV(object):

    LOCAL_TZ = pytz.timezone("America/New_York")
    REMOTE_TZ = None # auto detect

    def __init__(self, hostname, username, password, protocol='https'):
        self._dav = easywebdav.connect(hostname, username=username, password=password, protocol=protocol)

    def sync(self, localSource, remoteTarget):

        # First retrieve file listings for local and remote
        localFiles = set(os.listdir(localSource))
        remoteFiles = {urllib2.unquote(x.name.strip('/').split('/')[-1]) : x for x in self._dav.ls(remoteTarget)}

        if not self.REMOTE_TZ:
            self.REMOTE_TZ = pytz.timezone(remoteFiles.values()[0].mtime.split(' ')[-1])

        for fileName, meta in remoteFiles.iteritems():
            if fileName in localFiles:
                # File exists, we'll deal with this later
                pass
            elif meta.contenttype == 'httpd/unix-directory':
                # Directory doesn't exist,
                # we'll need to delete this recursively
                if fileName != remoteTarget.strip('/').split('/')[-1]:
                    # Make sure we don't delete the target!
                    self.rmtree(meta.name)
            else:
                # File doesn't exist, delete it
                print '[DAV] Deleting:', meta.name
                self._dav.delete(meta.name)

        for fileName in localFiles:
            localFile = '%s/%s' % (localSource, fileName)
            remoteFile = '%s/%s' % (remoteTarget, fileName)
            if os.path.isdir(localFile):
                if fileName in remoteFiles:
                    print '[DAV] %s already exists' % remoteFiles[fileName].name
                    self.sync(localFile, remoteFiles[fileName].name)
                else:
                    # Create directory
                    self._dav.mkdir(remoteFile)
                    print '[DAV] creating directory', remoteFile
                    self.sync(localFile, remoteFile)
            elif fileName in remoteFiles:
                # File exists but does it need updating?
                meta = remoteFiles[fileName]
                localFileMTime = self.LOCAL_TZ.localize(datetime.datetime(*time.gmtime(os.stat(localFile).st_mtime)[0:6]))
                remoteFileMTime = self.REMOTE_TZ.localize(datetime.datetime.strptime(meta.mtime, '%a, %d %b %Y %H:%M:%S %Z'))

                if remoteFileMTime < localFileMTime:
                    print '[DAV] Uploading: %s => %s' % (localFile, remoteFile)
                    self._dav.upload(localFile, meta.name)
                else:
                    print '[DAV] %s is already up to date' % meta.name
            else:
                # New file
                print '[DAV] Uploading (new): %s => %s' % (localFile, remoteFile)
                self._dav.upload(localFile, remoteFile)

    def rmtree(self, path):
        for file in self._dav.ls(path):
            if file.name == path:
                continue # Skip, the same file again
            elif file.contenttype == 'httpd/unix-directory':
                self.rmtree(path)
            else:
                print '[DAV] Deleting:', file.name
                self._dav.delete(file.name)

        print '[DAV] Deleting (dir):', path
        self._dav.rmdir(path)

class Generator(object):

    _MD_PLUGINS = ['extra', 'smarty', 'attr_list', 'headerid', 'tables', 'meta']
    _IMG_EXTENSIONS = ['png', 'jpg', 'jpeg', 'gif']
    _INDEX_HTML = """
                <div class="banner">
                    <a href="%(URL)s">
                        <img src="%(HEADER_IMAGE)s"/>
                        <h2>%(TITLE)s</h2>
                    </a>
                </div>
            """

    def __init__(self, templateDir, targetDir):
        self._templateDir = templateDir
        self._targetDir = targetDir
        self._data = list()
        self._markdown = markdown.Markdown(extensions = self._MD_PLUGINS)

        print 'Copying over base template'
        shutil.rmtree(self._targetDir, True)
        shutil.copytree(self._templateDir, self._targetDir)

        with open(self._template('template.html'), 'r') as f:
            self._templateHTML = f.read()

        self.loadData()
        self.compileSASS()
        self.generateContent()
        self.generateIndex()
        self.cleanupFiles()

    def _template(self, *args):
        return os.path.join(self._templateDir, *args)

    def _target(self, *args):
        return os.path.join(self._targetDir, *args)

    def _innerHTML(self, tree, xpath):

        try:
            node = tree.xpath(xpath)[0]
        except IndexError:
            return ''

        outerHTML = lxml.etree.tostring(node)
        innerRE = re.match(r'<[^>]*>(.*)<\/[^>]*>', outerHTML, re.DOTALL)

        if not innerRE:
            return ''

        return innerRE.groups()[0]

    def _findFile(self, base, extensions):
        for extension in extensions:
            path = '%s.%s' % (base, extension)
            if os.path.exists(path):
                return path

        raise ValueError('Unable to find any files matching: %s.[%s]' % (base, '|'.join(extensions)))

    def _HTMLminify(self, html):
        return htmlmin.minify.html_minify(html).encode('utf-8')

    def loadData(self):
        for file in sorted(glob.glob('*.md'), reverse=True):

            print 'Parsing:', file

            base = file.partition('.')[0]

            with open(file, 'r') as f:
                html = self._markdown.convert(f.read())
                tree = lxml.etree.parse(StringIO.StringIO(html), lxml.etree.HTMLParser())

            headerImage = self._findFile(os.path.join('imgs', base + '.head'), self._IMG_EXTENSIONS)
            footerImage = self._findFile(os.path.join('imgs', base + '.foot'), self._IMG_EXTENSIONS)
            headerImageName = headerImage.partition(os.path.sep)[2]
            footerImageName = footerImage.partition(os.path.sep)[2]

            self._data.append({
                'TITLE': self._innerHTML(tree, '//h1'),
                'INTRO': self._innerHTML(tree, '//header'),
                'CONTENT': self._innerHTML(tree, '//article').replace('[???', '<aside>').replace('???]', '</aside>'),
                'OUTTRO': self._innerHTML(tree, '//footer').replace('[???', '<aside>').replace('???]', '</aside>'),
                'HEADER_IMAGE': urllib2.quote('images/' + headerImageName),
                'FOOTER_IMAGE': urllib2.quote('images/' + footerImageName),
                'URL': urllib2.quote('/%s.html' % base),
                'F_HEADER_IMAGE': (headerImage, headerImageName, os.stat(headerImage)),
                'F_FOOTER_IMAGE': (footerImage, footerImageName, os.stat(headerImage)),
                'F_TARGET': base,
                'STATS': os.stat(file),
                'STATUS': self._markdown.Meta['status'][0].lower(),
                'SOURCE': """
                    <h2 id="source-material">Source Material</h2>
                    <p>
                        <a href="%(source.name.link)s"><img src="%(source.img)s" alt="%(source.name)s by %(source.author)s"/></a>
                    </p>
                    <p>
                        <a href="%(source.name.link)s">%(source.name)s</a> by <a href="%(source.author.link)s">%(source.author)s</a>
                    </p>
                """ % {
                        'source.img': self._markdown.Meta['source_cover'][0],
                        'source.name': self._markdown.Meta['source_title'][0],
                        'source.name.link': self._markdown.Meta['source_title'][1],
                        'source.author': self._markdown.Meta['source_author'][0],
                        'source.author.link': self._markdown.Meta['source_author'][1]
                } if 'source_title' in self._markdown.Meta else ''
            })

    def compileSASS(self):
        print 'Compiling SASS'
        with open(self._target('style.css'), 'w') as f:
            f.write(
                sass.compile(
                    output_style="compressed",
                    include_paths="html_template",
                    filename=self._template("style.scss")
                )
            )

    def generateContent(self):

        print 'Generating HTML files'

        for data in self._data:

            if data['STATUS'] == 'hidden':
                continue

            for imgPath, imgName, imgStats in (data['F_HEADER_IMAGE'], data['F_FOOTER_IMAGE']):
                subprocess.check_call(['jpegoptim', '-o', '-p', '-t', '--strip-all', '-m 50', '-T 1', imgPath])
                targetFile = self._target('images', imgName)
                shutil.copy(imgPath, targetFile)
                os.utime(targetFile, (imgStats.st_atime, imgStats.st_mtime))

            targetFile = self._target(data['F_TARGET'] + '.html')
            with open(targetFile, 'wb') as f:
                f.write(self._HTMLminify(self._templateHTML % data))
            os.utime(targetFile, (data['STATS'].st_atime, data['STATS'].st_mtime))

    def cleanupFiles(self):
        print 'Cleaning up files'
        for fileName in glob.glob(self._target('*.scss')):
            os.unlink(fileName)
        os.unlink(self._target('template.html'))

    def generateIndex(self):
        print 'Creating index'

        publishedHTML = ''
        draftHTML = ''

        for data in self._data:
            if data['STATUS'] == 'published':
                publishedHTML += self._INDEX_HTML % data
                draftHTML += self._INDEX_HTML % data
            if data['STATUS'] == 'draft':
                draftHTML += self._INDEX_HTML % data

        with open(self._template('index.html'), 'r') as fTemplate, \
             open(self._target('index.html'), 'w') as fOut, \
             open(self._target('index-draft.html'), 'w') as fOutDrafts:

                templateHTML = fTemplate.read()

                fOut.write(
                    self._HTMLminify(
                        templateHTML % {
                            'TITLE': 'Centreville Life Group',
                            'CONTENT': publishedHTML
                        }
                    )
                )

                fOutDrafts.write(
                    self._HTMLminify(
                        templateHTML % {
                            'TITLE': 'Centreville Life Group (drafts)',
                            'CONTENT': draftHTML
                        }
                    )
                )

# Check envrionment 

if not hasattr(sass, 'compile') and hasattr(sass, 'compile_file'):
    print 'Please install the SassPython instead of the sass package'
    sys.exit(1)

try:
    subprocess.check_output(['which', 'jpegoptim'])
except subprocess.CalledProcessError:
    print 'Please install jpegoptim'
    sys.exit(1)

if not os.path.exists('login.cfg'):
    print 'login.cfg not properly configured, see login.cfg.txt for an example'
    sys.exit(1)

config = ConfigParser.ConfigParser()
config.read('login.cfg')

Generator('html_template', '.html_output')

print 'Uploading ...'
dav = DAV(**{key:config.get('DAV',key) for key in config.options('DAV')})
dav.sync('.html_output', '/LifeGroup')
