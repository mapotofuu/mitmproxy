import abc
import typing  # noqa

import pyparsing as pp

from mitmproxy.utils import strutils
from . import actions, exceptions, base

LOG_TRUNCATE = 1024


class Message:
    __metaclass__ = abc.ABCMeta
    logattrs: typing.List[str] = []

    def __init__(self, tokens):
        track = set()
        for i in tokens:
            if i.unique_name:
                if i.unique_name in track:
                    raise exceptions.ParseException(
                        "Message has multiple %s clauses, "
                        "but should only have one." % i.unique_name,
                        0, 0
                    )
                else:
                    track.add(i.unique_name)
        self.tokens = tokens

    def strike_token(self, name):
        toks = [i for i in self.tokens if i.unique_name != name]
        return self.__class__(toks)

    def toks(self, klass):
        """
            Fetch all tokens that are instances of klass
        """
        return [i for i in self.tokens if isinstance(i, klass)]

    def tok(self, klass):
        """
            Fetch first token that is an instance of klass
        """
        l = self.toks(klass)
        if l:
            return l[0]

    def length(self, settings):
        """
            Calculate the length of the base message without any applied
            actions.
        """
        return sum(len(x) for x in self.values(settings))

    def preview_safe(self):
        """
            Return a copy of this message that is safe for previews.
        """
        tokens = [i for i in self.tokens if not isinstance(i, actions.PauseAt)]
        return self.__class__(tokens)

    def maximum_length(self, settings):
        """
            Calculate the maximum length of the base message with all applied
            actions.
        """
        l = self.length(settings)
        for i in self.actions:
            if isinstance(i, actions.InjectAt):
                l += len(i.value.get_generator(settings))
        return l

    @classmethod
    def expr(cls):  # pragma: no cover
        pass

    def log(self, settings):
        """
            A dictionary that should be logged if this message is served.
        """
        ret = {}
        for i in self.logattrs:
            v = getattr(self, i)
            # Careful not to log any VALUE specs without sanitizing them first.
            # We truncate at 1k.
            if hasattr(v, "values"):
                v = [x[:LOG_TRUNCATE] for x in v.values(settings)]
                v = strutils.bytes_to_escaped_str(b"".join(v))
            elif hasattr(v, "__len__"):
                v = v[:LOG_TRUNCATE]
                v = strutils.bytes_to_escaped_str(v)
            ret[i] = v
        ret["spec"] = self.spec()
        return ret

    def freeze(self, settings):
        r = self.resolve(settings)
        return self.__class__([i.freeze(settings) for i in r.tokens])

    def __repr__(self):
        return self.spec()


class NestedMessage(base.Token):
    """
        A nested message, as an escaped string with a preamble.
    """
    preamble = ""
    nest_type: typing.Optional[typing.Type[Message]] = None

    def __init__(self, value):
        super().__init__()
        self.value = value
        try:
            self.parsed = self.nest_type(
                self.nest_type.expr().parseString(
                    value.val.decode(),
                    parseAll=True
                )
            )
        except pp.ParseException as v:
            raise exceptions.ParseException(v.msg, v.line, v.col)

    @classmethod
    def expr(cls):
        e = pp.Literal(cls.preamble).suppress()
        e = e + base.TokValueLiteral.expr()
        return e.setParseAction(lambda x: cls(*x))

    def values(self, settings):
        return [
            self.value.get_generator(settings),
        ]

    def spec(self):
        return f"{self.preamble}{self.value.spec()}"

    def freeze(self, settings):
        f = self.parsed.freeze(settings).spec()
        return self.__class__(
            base.TokValueLiteral(
                strutils.bytes_to_escaped_str(f.encode(), escape_single_quotes=True)
            )
        )
