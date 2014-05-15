import random
import requests
import urlparse
import gevent
import time
from locust import events,Locust

BUFFERTIME = 10.0 # time to wait before playing
MAXMANIFESTAGE = 20.0
MAXRETRIES = 2

class HLSLocust(Locust):
    def __init__(self, *args, **kwargs):
        super(HLSLocust, self).__init__(*args, **kwargs)
        self.client = Player()

class Player():
    playlists=None
    queue = None
    # TODO, all attr should exist on these objects, rather than player object

    def __init__(self):
        pass

    def request(self,url,name=None):
        start_time = time.time()
        if name is None:
            name = url

        try:
            r = requests.get(url)
            r.raise_for_status() # requests wont raise http error for 404 otherwise
        except (requests.exceptions.ConnectionError,
                requests.exceptions.HTTPError, 
                requests.exceptions.Timeout,
                requests.exceptions.TooManyRedirects) as e:
            total_time = int((time.time() - start_time) * 1000)
            events.request_failure.fire(request_type="GET", name=name, 
                                        response_time=total_time, exception=e)
        else:
            total_time = int((time.time() - start_time) * 1000)
            try:
                response_length = int(r.headers['Content-Length'])
            except KeyError:
                response_length = 0
                
            events.request_success.fire(request_type="GET", name=name, 
                                        response_time=total_time, 
                                        response_length=response_length)
            return r
        return None

    def play(self, url=None, quality=None, duration=None):

        # forget 
        self.playlists = None
        self.queue = None

        baseUrl = url

        # request master playlist
        r = self.request(baseUrl)
        if r:
            self.parse(r.text)
        else: 
            return

        # currently I randomly pick a quality, unless it's given...
        if quality is None:
            playlist_url = urlparse.urljoin(baseUrl, random.choice(self.playlists).name)
        else:
            i = quality%len(self.playlist)
            playlist_url = urlparse.urljoin(baseUrl, self.playlists[i].name)

        # request media playlist
        r = self.request(playlist_url)
        if r:
            self.parse(r.text)
        else: 
            return

        start_time = None
        buffer_time = 0.0
        playing = False
        last_manifest_time = time.time()

        idx = 0
        retries = 0

        while True :
            # should I download an object?
            if idx < len(self.queue):
                a = self.queue[idx]
                url = urlparse.urljoin(baseUrl, a.name)
                r = self.request(url,'Segment ({url})'.format(url=playlist_url))
                if r:
                    idx+=1
                    buffer_time += a.duration
                else:
                    retries +=1
                    if retries >= MAXRETRIES:
                        play_time = 0
                        if start_time:
                            play_time = (time.time() - start_time)
                        return (buffer_time,play_time)


            # should we start playing?
            if not playing and buffer_time > BUFFERTIME: # TODO num segments?
                playing = True
                start_time = time.time()

            if playing:
                # should we grab a new manifest?
                manifest_age = (time.time() - last_manifest_time)
                if manifest_age > MAXMANIFESTAGE: # TODO, new manifest will fill downloaded files here
                    r = self.request(playlist_url)
                    last_manifest_time = time.time()
                    if r:
                        self.parse(r.text)

                play_time = (time.time() - start_time)
                # am I underrunning?
                if play_time > buffer_time:
                    if idx < len(self.queue):
                        # we've run out of buffer but we still have parts to download
                        raise ValueError # underrun
                    # we've finished a vod?
                    else :
                        return (buffer_time,play_time)
                # have we seen enough?
                if duration and play_time > duration :
                    return (buffer_time,play_time)
            gevent.sleep(1) # yield execution # TODO 1 second? to avoid 100% cpu?

    def parse(self,manifest):

        # remember old playlists
        oldPlaylists = self.playlists
        self.playlists = None

        lines = manifest.split('\n')
        for i,line in enumerate(lines):
            if line.startswith('#'):
                # TODO, fix parsing, I think EXT-X extents the previous EXT line
                if 'EXT-X-STREAM-INF' in line: # media playlist special case
                    if self.playlists is None:
                        self.playlists = [] # forget old playlists
                    key,val = line.split(':')
                    attr = myCast(val)
                    name = lines[i+1].rstrip() # next line 
                    self.playlists.append(MediaPlaylist(name,attr))

                if 'EXTINF' in line: # fragment special case
                    if self.queue is None:
                        self.queue = []
                    key,val = line.split(':')
                    attr = myCast(val)
                    name = lines[i+1].rstrip() # next line
                    if not name.startswith('#'):# TODO, bit of a hack here
                        if name not in [x.name for x in self.queue]:
                            self.queue.append(MediaFragment(name,attr))

                elif line.startswith('#EXT-X-'):
                    try:
                        key,val = line.split(':')
                    except ValueError:
                        key = line
                        val = True
                    key = attrName(key)
                    val = myCast(val)
                    setattr(self,key,val)

        # playlists weren't updated so keep old playlists
        if self.playlists == None:
            self.playlists = oldPlaylists

        return

class MasterPlaylist():
    pass

class MediaPlaylist():
    def __init__(self,name,attributes):
        self.name = name
        for k in attributes:
            setattr(self,k,attributes[k])

class MediaFragment():
    def __init__(self,name,attributes):
        self.name = name
        self.duration = attributes[0] # only attrib??

def myBool(a):
    if a.strip().lower()=='no':
        return False
    elif a.strip().lower()=='yes':
        return True
    raise ValueError

def myDict(a):
    a = list(mySplit(a))
    dct = {}
    for b in a:
        key,val = b.split('=')
        key = attrName(key)
        dct[key] = myCast(val)
    return dct

def myList(a):
    a = list(mySplit(a))
    if len(a)>1:
        return [myCast(x) for x in a]
    else:
        raise ValueError

def mySplit(string,sep=','):
    start = 0
    end = 0
    inString = False
    while end < len(string):
        if string[end] not in sep or inString: # mid string
            if string[end] in '\'\"':
                inString = not inString
            end +=1
        else: # separator
            yield string[start:end]
            end +=1
            start = end
    if start != end:# ignore empty items
        yield string[start:end]

def attrName(key):
    return key.replace('#EXT-X-','').replace('-','_').lower()

def myCast(val):
    # intelligent casting ish
    try:
        return int(val)
    except ValueError:
        pass

    try:
        return float(val)
    except ValueError:
        pass

    try:
        return myBool(val)
    except ValueError:
        pass

    try:
        return myDict(val)
    except ValueError:
        pass

    try:
        return myList(val)
    except ValueError:
        pass

    return val

