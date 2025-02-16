"""AnyNumberOf, OneOf, OptionallyBracketed & AnySetOf."""

from typing import FrozenSet, List, Optional
from typing import Sequence as SequenceType
from typing import Tuple, Union, cast

from sqlfluff.core.parser.context import ParseContext
from sqlfluff.core.parser.grammar.base import (
    BaseGrammar,
    cached_method_for_parse_context,
)
from sqlfluff.core.parser.grammar.sequence import Bracketed, Sequence
from sqlfluff.core.parser.helpers import trim_non_code_segments
from sqlfluff.core.parser.match_algorithms import greedy_match
from sqlfluff.core.parser.match_result import MatchResult
from sqlfluff.core.parser.match_wrapper import match_wrapper
from sqlfluff.core.parser.segments import BaseSegment, UnparsableSegment
from sqlfluff.core.parser.types import MatchableType, ParseMode, SimpleHintType


def _parse_mode_match_result(
    matched_segments: Tuple[BaseSegment, ...],
    unmatched_segments: Tuple[BaseSegment, ...],
    tail: Tuple[BaseSegment, ...],
    parse_mode: ParseMode,
) -> MatchResult:
    """A helper function for the return values of AnyNumberOf.

    This method creates UnparsableSegments as appropriate
    depending on the parse mode and return values.
    """
    # If we're being strict, just return.
    if parse_mode == ParseMode.STRICT:
        return MatchResult(matched_segments, unmatched_segments + tail)

    # Nothing in unmatched anyway?
    if not unmatched_segments or all(not s.is_code for s in unmatched_segments):
        return MatchResult(matched_segments, unmatched_segments + tail)

    _trim_idx = 0
    for _trim_idx in range(len(unmatched_segments)):
        if unmatched_segments[_trim_idx].is_code:
            break

    # Create an unmatched segment
    _expected = "Nothing else"
    if tail:
        _expected += f" before {tail[0].raw!r}"

    unmatched_seg = UnparsableSegment(
        unmatched_segments[_trim_idx:], expected=_expected
    )
    return MatchResult(
        matched_segments + unmatched_segments[:_trim_idx] + (unmatched_seg,),
        tail,
    )


class AnyNumberOf(BaseGrammar):
    """A more configurable version of OneOf."""

    supported_parse_modes = {
        ParseMode.STRICT,
        ParseMode.GREEDY,
    }

    def __init__(
        self,
        *args: Union[MatchableType, str],
        max_times: Optional[int] = None,
        min_times: int = 0,
        max_times_per_element: Optional[int] = None,
        exclude: Optional[MatchableType] = None,
        terminators: SequenceType[Union[MatchableType, str]] = (),
        reset_terminators: bool = False,
        allow_gaps: bool = True,
        optional: bool = False,
        parse_mode: ParseMode = ParseMode.STRICT,
    ) -> None:
        self.max_times = max_times
        self.min_times = min_times
        self.max_times_per_element = max_times_per_element
        # Any patterns to _prevent_ a match.
        self.exclude = exclude
        super().__init__(
            *args,
            allow_gaps=allow_gaps,
            optional=optional,
            terminators=terminators,
            reset_terminators=reset_terminators,
            parse_mode=parse_mode,
        )

    @cached_method_for_parse_context
    def simple(
        self, parse_context: ParseContext, crumbs: Optional[Tuple[str]] = None
    ) -> SimpleHintType:
        """Does this matcher support a uppercase hash matching route?

        AnyNumberOf does provide this, as long as *all* the elements *also* do.
        """
        option_simples: List[SimpleHintType] = [
            opt.simple(parse_context=parse_context, crumbs=crumbs)
            for opt in self._elements
        ]
        if any(elem is None for elem in option_simples):
            return None
        # We now know that there are no Nones.
        simple_buff = cast(List[Tuple[FrozenSet[str], FrozenSet[str]]], option_simples)
        # Combine the lists
        simple_raws = [simple[0] for simple in simple_buff if simple[0]]
        simple_types = [simple[1] for simple in simple_buff if simple[1]]
        return (
            frozenset.union(*simple_raws) if simple_raws else frozenset(),
            frozenset.union(*simple_types) if simple_types else frozenset(),
        )

    def is_optional(self) -> bool:
        """Return whether this element is optional.

        This is mostly set in the init method, but also in this
        case, if min_times is zero then this is also optional.
        """
        return self.optional or self.min_times == 0

    def _match_once(
        self, segments: Tuple[BaseSegment, ...], parse_context: ParseContext
    ) -> Tuple[MatchResult, Optional["MatchableType"]]:
        """Match the forward segments against the available elements once.

        This serves as the main body of OneOf, but also a building block
        for AnyNumberOf.
        """
        with parse_context.deeper_match(
            name=self.__class__.__name__,
            clear_terminators=self.reset_terminators,
            push_terminators=self.terminators,
        ) as ctx:
            match, matched_option = self._longest_trimmed_match(
                segments,
                self._elements,
                parse_context=ctx,
                trim_noncode=False,
            )

        return match, matched_option

    @match_wrapper()
    def match(
        self, segments: Tuple[BaseSegment, ...], parse_context: ParseContext
    ) -> MatchResult:
        """Match against any of the elements a relevant number of times.

        If it matches multiple, it returns the longest, and if any are the same
        length it returns the first (unless we explicitly just match first).
        """
        # First if we have an *exclude* option, we should check that
        # which would prevent the rest of this grammar from matching.
        if self.exclude:
            with parse_context.deeper_match(
                name=self.__class__.__name__ + "-Exclude"
            ) as ctx:
                if self.exclude.match(segments, ctx):
                    return MatchResult.from_unmatched(segments)

        matched_segments: MatchResult = MatchResult.from_empty()
        unmatched_segments: Tuple[BaseSegment, ...] = segments
        tail: Tuple[BaseSegment, ...] = ()

        # Secondly, if we're in a greedy mode, handle that first.
        if self.parse_mode == ParseMode.GREEDY:
            _terminators = [*self.terminators, *parse_context.terminators]
            _term_match = greedy_match(
                segments,
                parse_context,
                matchers=_terminators,
            )
            if _term_match:
                # If we found a terminator, trim off the tail of the available
                # segments to match on.
                unmatched_segments = _term_match.matched_segments
                tail = _term_match.unmatched_segments

        # Keep track of the number of times each option has been matched.
        n_matches = 0
        option_counter = {elem.cache_key(): 0 for elem in self._elements}

        while True:
            if self.max_times and n_matches >= self.max_times:
                # We've matched as many times as we can
                return _parse_mode_match_result(
                    matched_segments.matched_segments,
                    unmatched_segments,
                    tail,
                    self.parse_mode,
                )

            # Is there anything left to match?
            if len(unmatched_segments) == 0:
                # No...
                if n_matches >= self.min_times:
                    return _parse_mode_match_result(
                        matched_segments.matched_segments,
                        unmatched_segments,
                        tail,
                        self.parse_mode,
                    )
                else:  # pragma: no cover TODO?
                    # We didn't meet the hurdle
                    return MatchResult.from_unmatched(segments)

            # If we've already matched once...
            if n_matches > 0 and self.allow_gaps:
                # Consume any non-code if there is any
                pre_seg, mid_seg, post_seg = trim_non_code_segments(unmatched_segments)
                unmatched_segments = mid_seg + post_seg
            else:
                pre_seg = ()  # empty tuple

            match, matched_option = self._match_once(
                unmatched_segments, parse_context=parse_context
            )

            # Increment counter for matched option.
            if matched_option:
                matched_key = matched_option.cache_key()
                if matched_option.cache_key() in option_counter:
                    option_counter[matched_key] += 1
                    # Check if we have matched an option too many times.
                    if (
                        self.max_times_per_element
                        and option_counter[matched_key] > self.max_times_per_element
                    ):
                        return _parse_mode_match_result(
                            matched_segments.matched_segments,
                            pre_seg + unmatched_segments,
                            tail,
                            self.parse_mode,
                        )

            if match:
                matched_segments += pre_seg + match.matched_segments
                unmatched_segments = match.unmatched_segments
                n_matches += 1
            else:
                # If we get here, then we've not managed to match. And the next
                # unmatched segments are meaningful, i.e. they're not what we're
                # looking for.
                if n_matches >= self.min_times:
                    return _parse_mode_match_result(
                        matched_segments.matched_segments,
                        pre_seg + unmatched_segments,
                        tail,
                        self.parse_mode,
                    )
                else:
                    # We didn't meet the hurdle
                    return _parse_mode_match_result(
                        (),
                        matched_segments.matched_segments
                        + pre_seg
                        + unmatched_segments,
                        tail,
                        self.parse_mode,
                    )


class OneOf(AnyNumberOf):
    """Match any of the elements given once.

    If it matches multiple, it returns the longest, and if any are the same
    length it returns the first (unless we explicitly just match first).
    """

    def __init__(
        self,
        *args: Union[MatchableType, str],
        exclude: Optional[MatchableType] = None,
        terminators: SequenceType[Union[MatchableType, str]] = (),
        reset_terminators: bool = False,
        allow_gaps: bool = True,
        optional: bool = False,
        parse_mode: ParseMode = ParseMode.STRICT,
    ) -> None:
        super().__init__(
            *args,
            max_times=1,
            min_times=1,
            exclude=exclude,
            terminators=terminators,
            reset_terminators=reset_terminators,
            allow_gaps=allow_gaps,
            optional=optional,
            parse_mode=parse_mode,
        )


class OptionallyBracketed(OneOf):
    """Hybrid of Bracketed and Sequence: allows brackets but they aren't required.

    NOTE: This class is greedy on brackets so if they *can* be claimed, then
    they will be.
    """

    def __init__(
        self,
        *args: Union[MatchableType, str],
        exclude: Optional[MatchableType] = None,
        terminators: SequenceType[Union[MatchableType, str]] = (),
        reset_terminators: bool = False,
        optional: bool = False,
        parse_mode: ParseMode = ParseMode.STRICT,
    ) -> None:
        super().__init__(
            Bracketed(*args),
            # In the case that there is only one argument, no sequence is required.
            args[0] if len(args) == 1 else Sequence(*args),
            exclude=exclude,
            terminators=terminators,
            reset_terminators=reset_terminators,
            optional=optional,
            parse_mode=parse_mode,
        )


class AnySetOf(AnyNumberOf):
    """Match any number of the elements but each element can only be matched once."""

    def __init__(
        self,
        *args: Union[MatchableType, str],
        max_times: Optional[int] = None,
        min_times: int = 0,
        exclude: Optional[MatchableType] = None,
        terminators: SequenceType[Union[MatchableType, str]] = (),
        reset_terminators: bool = False,
        allow_gaps: bool = True,
        optional: bool = False,
        parse_mode: ParseMode = ParseMode.STRICT,
    ) -> None:
        super().__init__(
            *args,
            max_times_per_element=1,
            max_times=max_times,
            min_times=min_times,
            exclude=exclude,
            terminators=terminators,
            reset_terminators=reset_terminators,
            allow_gaps=allow_gaps,
            optional=optional,
            parse_mode=parse_mode,
        )
