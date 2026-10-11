"""Compose one URL path segment out of several validated query fields.

``render_path`` fills one ``{placeholder}`` from one validated query field, and a
:class:`~opendata.data.providers._engine.spec.ParamSpec` has no join or derive
field, so an SDMX data key built from several dimensions --
``/data/<agency>,<dataflow>,<version>/<freq>.<dim1>.<dim2>...`` -- had nowhere to
say how it is composed. This module is that missing declaration: a
:class:`CompositeKeySpec` names the separator, the ordered components, the
charset each component's text must belong to, and what happens when a component's
parameter is absent from the query (a declared default, or fail closed).

The composition works on *plain* values: a caller reads the validated fields off
its query object, passes a ``{param: value}`` mapping in, and gets one string
back, which is what the future ``ModelSpec`` seam will substitute into the path
template. Nothing here performs I/O, imports the engine's fetch plane, or mutates
a query; a declaration that cannot produce one safe segment raises
:class:`KeyCompositionError` with a stable reason instead of returning an empty
or garbage key.

Why a naive split is wrong: a ``.`` inside one field is legal -- the OECD agency
id ``OECD.SDD.NAD`` and the version ``1.1`` are each *one* dotted-id component,
and the dimension key joins its positions with ``.``. So the charset is declared
per component (:data:`SEGMENT_CHARSETS`, with :data:`CHARSET_SEPARATORS` saying
which separator each charset admits inside one field), and the segment-count
guard refuses the separator only where the declaration did not admit it. Every
refusal happens before any byte is sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

#: What fills a component position: a declared query parameter, or a constant.
ComponentSource = Literal["param", "literal"]

#: What an absent parameter means: fail closed, or substitute the declared default.
MissingPolicy = Literal["refuse", "default"]

#: The separator no component may ever inject. A ``/`` in one component would
#: open a second URL path segment from inside one declared key, so it is refused
#: for every component regardless of the charset its declaration names.
_PATH_SEPARATOR = "/"

#: A declaration's stable error namespace when it wants refusals attributed to
#: itself (a model passes ``error_code="OECD_KEY_COMPOSITION_INVALID"``).
DEFAULT_ERROR_CODE = "KEY_COMPOSITION_INVALID"

#: The charsets a component declaration may name, each as one whole-string
#: pattern. ``sdmx_code`` is the dimension-shape grammar -- alphanumerics, ``_``,
#: ``-``, ``@``, no dots -- so a code carrying a separator or a slash is refused
#: rather than silently becoming two positions. ``path_segment`` mirrors the
#: engine's own single-segment grammar at ``http_json._PATH_SEGMENT``.
#: ``dotted_id`` admits ``.`` only *between* code runs, which is exactly the
#: shape of ``OECD.SDD.NAD`` or ``1.1``: one field that legally contains dots
#: without being several fields.
SEGMENT_CHARSETS: dict[str, re.Pattern[str]] = {
    "sdmx_code": re.compile(r"[A-Za-z0-9_\-@]+\Z"),
    "path_segment": re.compile(r"[A-Za-z0-9._~\-]+\Z"),
    "dotted_id": re.compile(r"[A-Za-z0-9_\-@]+(?:\.[A-Za-z0-9_\-@]+)*\Z"),
}

#: Which separator each charset admits *inside* one component. ``sdmx_code``
#: admits none, so any injected separator is the segment-count guard's business;
#: the dotted charsets admit ``.`` by declaration, and a composition joining on
#: another separator (the resource-comma shape, or a ``+`` key list) enforces
#: count even for them because the guard compares against *this* separator.
CHARSET_SEPARATORS: dict[str, frozenset[str]] = {
    "sdmx_code": frozenset(),
    "path_segment": frozenset("."),
    "dotted_id": frozenset("."),
}

#: The policies :class:`KeyComponent` may take for a component absent from the query.
MISSING_POLICIES = frozenset({"refuse", "default"})


class KeyCompositionError(ValueError):
    """A composite key declaration cannot produce one safe path segment here.

    Attributes:
        code: Stable failure code, carried from the declaring
            :attr:`CompositeKeySpec.error_code`, so refusals are attributable
            without parsing prose.
        reason: Machine-readable name of the guard that fired
            (``"separator_injection"``, ``"path_traversal"``,
            ``"charset_invalid"``, ``"missing_component"``, ``"empty_component"``,
            ``"line_break"``, ``"unrenderable_bool"``), kept as an attribute so a
            test can pin *which* guard refused.
        component: The component name the refusal is about, when one component
            is at fault.
    """

    def __init__(self, code: str, reason: str, *, component: str = "", detail: str = "") -> None:
        """Store the code, the guard that fired and the component it fired on."""
        self.code = code
        self.reason = reason
        self.component = component
        self.detail = detail
        where = "" if not component else f" component={component!r}"
        super().__init__(f"{code} reason={reason}{where} {detail}".strip())


def _refuse(reason: str, component: str = "", detail: str = "") -> None:
    """Raise the module's own domain error; no refusal is a returned value."""
    raise KeyCompositionError(DEFAULT_ERROR_CODE, reason, component=component, detail=detail)


def _render_text(value: object) -> str:
    """Render one validated query value the way a composite key position spells it.

    ``date`` renders ISO because that is the string the upstream publishes its
    dimension values in. A boolean is refused outright: ``True``/``False`` would
    match an alphanumeric charset, and a flag silently landing in a resource key
    is exactly the garbage this kernel exists to refuse.
    """
    if isinstance(value, bool):
        _refuse("unrenderable_bool")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class KeyComponent:
    """One position inside a composite key.

    Attributes:
        name: Review label; refusals are attributed by it.
        source: What fills the position: the name of the query parameter this
            component reads when ``kind`` is ``"param"``, or the constant text
            itself when ``kind`` is ``"literal"`` (``S1``, ``B1GQ``, ``CPI``).
            A ``"param"`` component with an empty ``source`` reads nothing and
            always takes its declared default -- SDMX's fixed wildcard and
            constant positions.
        kind: ``"param"`` reads the validated query; ``"literal"`` is fixed text
            the model always publishes.
        charset: Name into :data:`SEGMENT_CHARSETS`; text is matched whole -- at
            declaration time for literals, at render time for query values.
        on_missing: What an absent parameter means: ``"refuse"`` fails closed,
            ``"default"`` substitutes ``default``. SDMX wildcard positions are
            declared this way, never inferred from a missing key.
        default: The substituted text when ``on_missing`` is ``"default"``. The
            empty string is legal and means one wildcard position (the ``..``
            inside ``{freq}..{area}``); any other default is validated against
            ``charset`` at construction, so no fallback text can carry a
            separator or a slash into the key.
        note: Why the bound exists, for the review record.
    """

    name: str
    source: str = ""
    kind: ComponentSource = "param"
    charset: str = "sdmx_code"
    on_missing: MissingPolicy = "refuse"
    default: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        """Reject a component declaration this kernel could not honour."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a key component needs a non-blank review name")
        if self.kind not in ("param", "literal"):
            raise ValueError(f"component {self.name!r} has unknown kind {self.kind!r}")
        if self.charset not in SEGMENT_CHARSETS:
            raise ValueError(
                f"component {self.name!r} names charset {self.charset!r}, not one of "
                f"{sorted(SEGMENT_CHARSETS)}"
            )
        if self.on_missing not in MISSING_POLICIES:
            raise ValueError(f"component {self.name!r} has unknown on_missing {self.on_missing!r}")
        if self.kind == "literal":
            self._check_literal()
            return
        if not isinstance(self.source, str):
            raise ValueError(f"component {self.name!r} source must be a string")
        if self.source and not self.source.isidentifier():
            raise ValueError(
                f"component {self.name!r} must name the query parameter it reads as source"
            )
        if not self.source and self.on_missing != "default":
            raise ValueError(
                f"component {self.name!r} reads no parameter, so it must declare a default"
            )
        self._check_default()

    def _check_literal(self) -> None:
        """A literal is fixed text: its charset must accept it, whole, at declaration."""
        if not isinstance(self.source, str) or not self.source:
            raise ValueError(f"literal component {self.name!r} declares no text")
        if not SEGMENT_CHARSETS[self.charset].match(self.source):
            raise ValueError(
                f"literal component {self.name!r} text {self.source!r} is not "
                f"valid {self.charset!r}"
            )
        if self.default is not None:
            raise ValueError(f"literal component {self.name!r} has no missing state to default")

    def _check_default(self) -> None:
        """Resolve the missing-policy pair: refuse declares no default, default needs text."""
        if self.on_missing == "default":
            if self.default is None or not isinstance(self.default, str):
                raise ValueError(
                    f"component {self.name!r} declares on_missing='default' "
                    "without string default text"
                )
            if self.default == "":
                return  # an explicitly declared SDMX wildcard position
            if not SEGMENT_CHARSETS[self.charset].match(self.default):
                raise ValueError(
                    f"component {self.name!r} default {self.default!r} is not "
                    f"valid {self.charset!r} text"
                )
        elif self.default is not None:
            raise ValueError(f"component {self.name!r} declares a default but on_missing='refuse'")


@dataclass(frozen=True)
class CompositeKeySpec:
    """A path segment declared as ``separator.join(components)``.

    A :class:`~opendata.data.providers._engine.spec.ModelSpec` field will hold
    one of these; the declaration is inert data until
    :func:`render_composite_key` (or :meth:`render`) runs it against plain
    values.

    Attributes:
        name: Review label for the composition (``"dimension_key"``).
        separator: The one character joining positions: ``"."`` for an SDMX data
            key, ``","`` for a resource key. Never URL structure.
        components: The ordered positions, at least one.
        error_code: Stable code carried by every refusal this composition raises.
    """

    name: str
    separator: str = "."
    components: tuple[KeyComponent, ...] = ()
    error_code: str = DEFAULT_ERROR_CODE

    def __post_init__(self) -> None:
        """Reject a composition that could not describe one segment."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("a composite key needs a non-blank review name")
        if not isinstance(self.components, tuple) or not self.components:
            raise ValueError(f"composite key {self.name!r} declares no components")
        for component in self.components:
            if not isinstance(component, KeyComponent):
                raise ValueError(f"composite key {self.name!r} has a non-KeyComponent entry")
        if not isinstance(self.separator, str) or len(self.separator) != 1:
            raise ValueError(f"composite key {self.name!r} separator must be exactly one character")
        if self.separator in (_PATH_SEPARATOR, "?", "#", "\r", "\n"):
            raise ValueError(
                f"composite key {self.name!r} cannot join on {self.separator!r}: "
                "it is URL structure, not a key separator"
            )
        if not isinstance(self.error_code, str) or not self.error_code.isupper():
            raise ValueError(f"composite key {self.name!r} error_code must be upper-case")

    @property
    def segment_count(self) -> int:
        """How many separator-delimited positions a rendered key of this shape has.

        A component whose text carries the separator, where its declared charset
        did not admit one, would raise this count from inside a single field --
        the injection-shaped bug :meth:`render` refuses as
        ``"separator_injection"``.
        """
        return len(self.components)

    def render(self, values: Mapping[str, object]) -> str:
        """Compose this key from plain values and return the joined segment.

        ``values`` maps a component's ``source`` parameter name to the validated
        query value (``str``, ``int`` or ``date``); a name that maps to ``None``
        or is absent is the component being missing, and the component's own
        ``on_missing`` policy decides between its declared default and failing
        closed. Ints render by ``str`` (a paging-size dimension), dates ISO.

        Raises:
            KeyCompositionError: A component is missing under a refusing policy,
                or its text would change the number of positions: injecting the
                separator, opening a second path segment, carrying a line break,
                being empty, or lying outside its declared charset.
        """
        parts = [self._render_part(component, values) for component in self.components]
        return self.separator.join(parts)

    def _refuse(self, reason: str, component: str = "", detail: str = "") -> None:
        """Raise this composition's attributed domain error."""
        raise KeyCompositionError(self.error_code, reason, component=component, detail=detail)

    def _render_part(self, component: KeyComponent, values: Mapping[str, object]) -> str:
        """Resolve and validate one position."""
        if component.kind == "literal":
            return component.source
        raw = values.get(component.source) if component.source else None
        if raw is None:
            if component.on_missing == "default":
                return component.default if component.default is not None else ""
            self._refuse(
                "missing_component",
                component.name,
                f"parameter {component.source!r} has no value and declares no default",
            )
        try:
            text = _render_text(raw)
        except KeyCompositionError as error:
            self._refuse(error.reason, component.name)
        if not text:
            self._refuse("empty_component", component.name)
        if any(character in text for character in "\r\n"):
            self._refuse("line_break", component.name)
        if _PATH_SEPARATOR in text:
            self._refuse("path_traversal", component.name)
        if self.separator in text and self.separator not in CHARSET_SEPARATORS[component.charset]:
            self._refuse(
                "separator_injection",
                component.name,
                f"a {self.separator!r} inside one field would raise the key past "
                f"{self.segment_count} positions",
            )
        if not SEGMENT_CHARSETS[component.charset].match(text):
            self._refuse(
                "charset_invalid",
                component.name,
                f"text is not valid {component.charset!r}",
            )
        return text


def render_composite_key(spec: CompositeKeySpec, values: Mapping[str, object]) -> str:
    """Compose one declared key from plain values: the plain-function face of the kernel.

    Provided so a caller -- and the future ``ModelSpec`` seam -- passes a mapping
    in and gets one string out without reaching through the dataclass. Refusals
    are :class:`KeyCompositionError`, raised by :meth:`CompositeKeySpec.render`;
    this function never returns an empty or garbage key.
    """
    return spec.render(values)
