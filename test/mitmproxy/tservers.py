import os.path
import threading
import tempfile
import sys
import time
from unittest import mock
import asyncio

import mitmproxy.platform
from mitmproxy.addons import core
from mitmproxy.proxy.config import ProxyConfig
from mitmproxy.proxy.server import ProxyServer
from mitmproxy import controller
from mitmproxy import options
from mitmproxy import exceptions
from mitmproxy import io
from mitmproxy.utils import human
import pathod.test
import pathod.pathoc

from mitmproxy import eventsequence
from mitmproxy.test import tflow
from mitmproxy.test import tutils
from mitmproxy.test import taddons


class MasterTest:

    async def cycle(self, master, content):
        f = tflow.tflow(req=tutils.treq(content=content))
        layer = mock.Mock("mitmproxy.proxy.protocol.base.Layer")
        layer.client_conn = f.client_conn
        layer.reply = controller.DummyReply()
        await master.addons.handle_lifecycle("clientconnect", layer)
        for i in eventsequence.iterate(f):
            await master.addons.handle_lifecycle(*i)
        await master.addons.handle_lifecycle("clientdisconnect", layer)
        return f

    async def dummy_cycle(self, master, n, content):
        for i in range(n):
            await self.cycle(master, content)
        await master._shutdown()

    def flowfile(self, path):
        with open(path, "wb") as f:
            fw = io.FlowWriter(f)
            t = tflow.tflow(resp=True)
            fw.add(t)


class TestState:
    def __init__(self):
        self.flows = []

    def request(self, f):
        if f not in self.flows:
            self.flows.append(f)

    def response(self, f):
        if f not in self.flows:
            self.flows.append(f)

    def websocket_start(self, f):
        if f not in self.flows:
            self.flows.append(f)


class TestMaster(taddons.RecordingMaster):

    def __init__(self, opts):
        super().__init__(opts)
        config = ProxyConfig(opts)
        self.server = ProxyServer(config)

    def clear_addons(self, addons):
        self.addons.clear()
        self.state = TestState()
        self.addons.add(self.state)
        self.addons.add(*addons)
        self.addons.trigger("configure", self.options.keys())
        self.addons.trigger("running")

    def reset(self, addons):
        self.clear_addons(addons)
        self.clear()


class ProxyThread(threading.Thread):

    def __init__(self, masterclass, options):
        threading.Thread.__init__(self)
        self.masterclass = masterclass
        self.options = options
        self.tmaster = None
        self.event_loop = None
        controller.should_exit = False

    @property
    def port(self):
        return self.tmaster.server.address[1]

    @property
    def tlog(self):
        return self.tmaster.logs

    def shutdown(self):
        self.tmaster.shutdown()

    def run(self):
        self.event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.event_loop)
        self.tmaster = self.masterclass(self.options)
        self.tmaster.addons.add(core.Core())
        self.name = "ProxyThread (%s)" % human.format_address(self.tmaster.server.address)
        self.tmaster.run()

    def set_addons(self, *addons):
        self.tmaster.reset(addons)

    def start(self):
        super().start()
        while True:
            if self.tmaster:
                break
            time.sleep(0.01)


class ProxyTestBase:
    # Test Configuration
    ssl = None
    ssloptions = False
    masterclass = TestMaster

    add_upstream_certs_to_client_chain = False

    @classmethod
    def setup_class(cls):
        cls.server = pathod.test.Daemon(
            ssl=cls.ssl,
            ssloptions=cls.ssloptions)
        cls.server2 = pathod.test.Daemon(
            ssl=cls.ssl,
            ssloptions=cls.ssloptions)

        cls.options = cls.get_options()
        cls.proxy = ProxyThread(cls.masterclass, cls.options)
        cls.proxy.start()

    @classmethod
    def teardown_class(cls):
        # perf: we want to run tests in parallel
        # should this ever cause an error, travis should catch it.
        # shutil.rmtree(cls.confdir)
        cls.proxy.shutdown()
        cls.server.shutdown()
        cls.server2.shutdown()

    def teardown(self):
        try:
            self.server.wait_for_silence()
        except exceptions.Timeout:
            # FIXME: Track down the Windows sync issues
            if sys.platform != "win32":
                raise

    def setup(self):
        self.master.reset(self.addons())
        self.server.clear_log()
        self.server2.clear_log()

    @property
    def master(self):
        return self.proxy.tmaster

    @classmethod
    def get_options(cls):
        cls.confdir = os.path.join(tempfile.gettempdir(), "mitmproxy")
        return options.Options(
            listen_port=0,
            confdir=cls.confdir,
            add_upstream_certs_to_client_chain=cls.add_upstream_certs_to_client_chain,
            ssl_insecure=True,
        )

    def set_addons(self, *addons):
        self.proxy.set_addons(*addons)

    def addons(self):
        """
            Can be over-ridden to add a standard set of addons to tests.
        """
        return []


class LazyPathoc(pathod.pathoc.Pathoc):
    def __init__(self, lazy_connect, *args, **kwargs):
        self.lazy_connect = lazy_connect
        pathod.pathoc.Pathoc.__init__(self, *args, **kwargs)

    def connect(self):
        return pathod.pathoc.Pathoc.connect(self, self.lazy_connect)


class HTTPProxyTest(ProxyTestBase):

    def pathoc_raw(self):
        return pathod.pathoc.Pathoc(("127.0.0.1", self.proxy.port), fp=None)

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        if self.ssl:
            conn = ("127.0.0.1", self.server.port)
        else:
            conn = None
        return LazyPathoc(
            conn,
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )

    def pathod(self, spec, sni=None):
        """
            Constructs a pathod GET request, with the appropriate base and proxy.
        """
        p = self.pathoc(sni=sni)
        if self.ssl:
            q = "get:'/p/%s'" % spec
        else:
            q = f"get:'{self.server.urlbase}/p/{spec}'"
        with p.connect():
            return p.request(q)

    def app(self, page):
        if self.ssl:
            p = pathod.pathoc.Pathoc(
                ("127.0.0.1", self.proxy.port), True, fp=None
            )
            with p.connect((self.master.options.onboarding_host, self.master.options.onbarding_port)):
                return p.request("get:'%s'" % page)
        else:
            p = self.pathoc()
            with p.connect():
                return p.request(f"get:'http://{self.master.options.onboarding_host}{page}'")


class TransparentProxyTest(ProxyTestBase):
    ssl = None

    @classmethod
    def setup_class(cls):
        cls._init_transparent_mode = mitmproxy.platform.init_transparent_mode
        cls._original_addr = mitmproxy.platform.original_addr
        mitmproxy.platform.init_transparent_mode = lambda: True
        mitmproxy.platform.original_addr = lambda sock: ("127.0.0.1", cls.server.port)
        super().setup_class()

    @classmethod
    def teardown_class(cls):
        super().teardown_class()
        mitmproxy.platform.init_transparent_mode = cls._init_transparent_mode
        mitmproxy.platform.original_addr = cls._original_addr

    @classmethod
    def get_options(cls):
        opts = ProxyTestBase.get_options()
        opts.mode = "transparent"
        return opts

    def pathod(self, spec, sni=None):
        """
            Constructs a pathod GET request, with the appropriate base and proxy.
        """
        if self.ssl:
            p = self.pathoc(sni=sni)
            q = "get:'/p/%s'" % spec
        else:
            p = self.pathoc()
            q = "get:'/p/%s'" % spec
        with p.connect():
            return p.request(q)

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = pathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        return p


class ReverseProxyTest(ProxyTestBase):
    ssl = None

    @classmethod
    def get_options(cls):
        opts = ProxyTestBase.get_options()
        s = "".join(
            [
                "https" if cls.ssl else "http",
                "://",
                "127.0.0.1:",
                str(cls.server.port)
            ]
        )
        opts.mode = "reverse:" + s
        return opts

    def pathoc(self, sni=None):
        """
            Returns a connected Pathoc instance.
        """
        p = pathod.pathoc.Pathoc(
            ("localhost", self.proxy.port), ssl=self.ssl, sni=sni, fp=None
        )
        return p

    def pathod(self, spec, sni=None):
        """
            Constructs a pathod GET request, with the appropriate base and proxy.
        """
        if self.ssl:
            p = self.pathoc(sni=sni)
            q = "get:'/p/%s'" % spec
        else:
            p = self.pathoc()
            q = "get:'/p/%s'" % spec
        with p.connect():
            return p.request(q)


class SocksModeTest(HTTPProxyTest):

    @classmethod
    def get_options(cls):
        opts = ProxyTestBase.get_options()
        opts.mode = "socks5"
        return opts


class HTTPUpstreamProxyTest(HTTPProxyTest):
    """
    Chain three instances of mitmproxy in a row to test upstream mode.
    Proxy order is cls.proxy -> cls.chain[0] -> cls.chain[1]
    cls.proxy and cls.chain[0] are in upstream mode,
    cls.chain[1] is in regular mode.
    """
    chain = None
    n = 2

    @classmethod
    def setup_class(cls):
        # We need to initialize the chain first so that the normal server gets a correct config.
        cls.chain = []
        for _ in range(cls.n):
            opts = cls.get_options()
            proxy = ProxyThread(cls.masterclass, opts)
            proxy.start()
            cls.chain.insert(0, proxy)
            while True:
                if proxy.event_loop and proxy.event_loop.is_running():
                    break

        super().setup_class()

    @classmethod
    def teardown_class(cls):
        super().teardown_class()
        for proxy in cls.chain:
            proxy.shutdown()

    def setup(self):
        super().setup()
        for proxy in self.chain:
            proxy.tmaster.reset(self.addons())

    @classmethod
    def get_options(cls):
        opts = super().get_options()
        if cls.chain:  # First proxy is in normal mode.
            s = "http://127.0.0.1:%s" % cls.chain[0].port
            opts.update(
                mode="upstream:" + s,
            )
        return opts
