# Python standard library notes

re.findall(pattern, string) returns every non overlapping match as a list of strings. With one capture group it returns the group, with several it returns tuples.

re.sub(pattern, replacement, string) replaces every match. The replacement can be a function taking the match object, which is how you do a computed substitution.

re.split(pattern, string) splits on a pattern rather than a fixed separator. A capturing group keeps the separators in the result.

re.match anchors at the start of the string, re.search looks anywhere, and re.fullmatch requires the whole string to match.

str.split() with no argument splits on any run of whitespace and drops empty strings, which is different from str.split(" ").

str.partition(sep) returns a three part tuple of before, separator and after, and never raises when the separator is absent.

str.translate with str.maketrans maps or deletes many characters in one pass, which is faster than chained replace calls.

str.removeprefix and str.removesuffix strip an exact affix and return the string unchanged when it is not there.

itertools.chain links several iterables end to end. itertools.chain.from_iterable does the same for an iterable of iterables.

itertools.groupby groups consecutive equal items. The input must be sorted by the same key or groups repeat.

itertools.accumulate produces running totals, or running anything when given a binary function.

itertools.pairwise yields overlapping pairs, so a list of n items gives n minus one pairs.

itertools.product is nested loops as an iterator, and repeat gives the same iterable several times.

collections.Counter counts hashable items and most_common returns them ordered by count.

collections.defaultdict calls a factory for a missing key instead of raising, which removes the setdefault dance.

collections.deque appends and pops from both ends in constant time, and maxlen makes a sliding window.

functools.lru_cache memoises a function on its arguments. functools.cache is the same with no size limit.

functools.reduce folds a binary function over an iterable, with an optional initial value.

functools.partial fixes some arguments of a function and returns a new callable.

heapq turns a list into a min heap in place. nlargest and nsmallest take a key function.

bisect.insort keeps a sorted list sorted on insert, and bisect_left finds the insertion point.

math.isclose compares floats with a relative tolerance, which is what you want instead of equality.

math.gcd and math.lcm take any number of integers. divmod returns the quotient and remainder together.

datetime.timedelta represents a duration. Subtracting two datetimes gives one, and it has total_seconds.

datetime.fromisoformat parses the ISO 8601 strings that isoformat produces, including offsets.

json.dumps with sort_keys gives a stable string. The default argument handles types json does not know.

dataclasses.dataclass generates init, repr and eq. frozen makes instances hashable and immutable.

enum.Enum gives named constants. StrEnum members compare equal to their string values.

pathlib.Path joins with the slash operator, and read_text and write_text handle opening and closing.

typing.Protocol describes a shape rather than a base class, so any object with the right methods satisfies it.

contextlib.contextmanager turns a generator with one yield into a with statement context manager.

textwrap.dedent removes the common leading whitespace from a triple quoted block.

unittest.mock is rarely the answer in tests. Prefer passing a real object built for the test.

sorted takes a key function and is stable, so sorting twice orders by the second key within the first.

zip with strict=True raises when the iterables differ in length instead of silently stopping short.
