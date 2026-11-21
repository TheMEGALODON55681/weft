"""The page cache and conditional requests, against the fixture site."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from fixture_site import FixtureSite
from weft.config import Settings
from weft.db import Database
from weft.fetch.cache import PageCache
from weft.fetch.http import Fetcher
from weft.fetch.page import get_page


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    d = await Database.open(tmp_path / "weft.db")
    yield d
    await d.close()


@pytest.fixture
async def setup(db: Database) -> AsyncIterator[Callable[..., tuple[Fetcher, PageCache, Clock]]]:
    fetchers: list[Fetcher] = []

    def make(ttl: float = 60.0) -> tuple[Fetcher, PageCache, Clock]:
        clock = Clock()
        fetcher = Fetcher(Settings())
        fetchers.append(fetcher)
        return fetcher, PageCache(db, ttl, clock=clock), clock

    yield make
    for f in fetchers:
        await f.aclose()


async def test_a_fresh_entry_is_served_without_contacting_the_site(make_site, setup) -> None:
    site: FixtureSite = make_site()
    fetcher, cache, _ = setup()
    first = await get_page(fetcher, Settings(), site.url("/article"), cache=cache)
    again = await get_page(fetcher, Settings(), site.url("/article?utm_source=x"), cache=cache)
    assert site.hits("/article") == 1
    assert not first.cached and first.body_bytes > 0
    assert again.cached and again.body_bytes == 0
    assert again.url == site.url("/article?utm_source=x")  # the URL as asked, not the key
    assert again.markdown == first.markdown
    assert again.final_url == first.final_url


async def test_a_stale_entry_is_revalidated_with_its_etag(make_site, setup) -> None:
    site: FixtureSite = make_site()
    fetcher, cache, clock = setup(ttl=60)
    first = await get_page(fetcher, Settings(), site.url("/etag"), cache=cache)
    clock.now += 61
    again = await get_page(fetcher, Settings(), site.url("/etag"), cache=cache)
    assert site.hits("/etag") == 2  # one download, one 304
    assert again.cached and again.status == 200
    assert again.markdown == first.markdown
    # The 304 renewed the entry, so the next call does not even revalidate.
    await get_page(fetcher, Settings(), site.url("/etag"), cache=cache)
    assert site.hits("/etag") == 2


async def test_last_modified_revalidates_too(make_site, setup) -> None:
    site: FixtureSite = make_site()
    fetcher, cache, clock = setup(ttl=60)
    await get_page(fetcher, Settings(), site.url("/lastmod"), cache=cache)
    clock.now += 61
    again = await get_page(fetcher, Settings(), site.url("/lastmod"), cache=cache)
    assert again.cached
    assert site.hits("/lastmod") == 2


async def test_a_stale_entry_without_validators_is_fetched_again(make_site, setup) -> None:
    site: FixtureSite = make_site()
    fetcher, cache, clock = setup(ttl=60)
    await get_page(fetcher, Settings(), site.url("/article"), cache=cache)
    clock.now += 61
    again = await get_page(fetcher, Settings(), site.url("/article"), cache=cache)
    assert not again.cached
    assert site.hits("/article") == 2


async def test_error_pages_are_not_cached(make_site, setup) -> None:
    site: FixtureSite = make_site()
    fetcher, cache, _ = setup()
    for _ in range(2):
        p = await get_page(fetcher, Settings(), site.url("/status/404"), cache=cache)
        assert not p.cached
    assert site.hits("/status/404") == 2


async def test_render_modes_have_separate_entries(make_site, setup) -> None:
    """A page cached as plain HTTP must not answer a call that asked for a browser."""
    site: FixtureSite = make_site()
    fetcher, cache, _ = setup()
    await get_page(fetcher, Settings(), site.url("/article"), render="never", cache=cache)
    p = await get_page(fetcher, Settings(), site.url("/article"), render="auto", cache=cache)
    assert not p.cached
    assert site.hits("/article") == 2


async def test_a_zero_ttl_turns_the_cache_off(make_site, setup) -> None:
    site: FixtureSite = make_site()
    fetcher, cache, _ = setup(ttl=0)
    for _ in range(2):
        await get_page(fetcher, Settings(), site.url("/article"), cache=cache)
    assert site.hits("/article") == 2
