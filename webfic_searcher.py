"""
TODO: Double-check necessary permissions.
TODO: Double-check necessary intents.
TODO: Evaluate method of input for auth details.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import textwrap
import tomllib
from collections.abc import AsyncGenerator, Callable, Sequence
from pathlib import Path
from typing import Any, Literal, NamedTuple, Self, TypeAlias

import aiohttp
import ao3
import apsw
import apsw.bestpractice
import atlas_api
import discord
import fichub_api
import platformdirs
import xxhash


try:
    import uvloop  # type: ignore
except ModuleNotFoundError:
    uvloop = None

# Set up logging.
discord.utils.setup_logging()
apsw.bestpractice.apply(apsw.bestpractice.recommended)  # type: ignore # SQLite WAL mode, logging, and other things.
log = logging.getLogger(__name__)

platformdir_info = platformdirs.PlatformDirs("discord-webfic-searcher", "Sachaa-Thanasius", roaming=False)

StoryDataType: TypeAlias = atlas_api.Story | fichub_api.Story | ao3.Work | ao3.Series

INITIALIZATION_STATEMENT = """
CREATE TABLE IF NOT EXISTS webfic_autoresponse_settings (
    guild_id    INTEGER     NOT NULL,
    channel_id  INTEGER     NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
) STRICT, WITHOUT ROWID;
"""

SELECT_ALL_STATEMENT = """
SELECT * FROM webfic_autoresponse_settings;
"""

SELECT_BY_GUILD_STATEMENT = """
SELECT * FROM webfic_autoresponse_settings WHERE guild_id = ?;
"""

INSERT_CHANNEL_STATEMENT = """
INSERT INTO webfic_autoresponse_settings (guild_id, channel_id)
VALUES (?, ?)
ON CONFLICT (guild_id, channel_id) DO NOTHING;
"""

REMOVE_GUILD_CHANNEL_STATEMENT = """
DELETE FROM fanfic_autoresponse_settings WHERE channel_id = ?;
"""

CLEAR_GUILD_CHANNELS_STATEMENT = """
DELETE FROM fanfic_autoresponse_settings WHERE guild_id = ?;
"""


class AutoresponseLocation(NamedTuple):
    guild_id: int
    channel_id: int


def _setup_db(conn: apsw.Connection) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(INITIALIZATION_STATEMENT)


def _query(conn: apsw.Connection, query_str: str, params: apsw.Bindings | None = None) -> list[AutoresponseLocation]:
    with conn:
        cursor = conn.cursor()
        return [AutoresponseLocation(*row) for row in cursor.execute(query_str, params)]


def _add(conn: apsw.Connection, locations: Sequence[AutoresponseLocation]) -> list[AutoresponseLocation]:
    with conn:
        cursor = conn.cursor()
        cursor.executemany(INSERT_CHANNEL_STATEMENT, locations)
        return [
            AutoresponseLocation(*row) for row in cursor.execute(SELECT_BY_GUILD_STATEMENT, (locations[0].guild_id,))
        ]


def _drop(conn: apsw.Connection, locations: Sequence[AutoresponseLocation]) -> list[AutoresponseLocation]:
    with conn:
        cursor = conn.cursor()
        cursor.executemany(REMOVE_GUILD_CHANNEL_STATEMENT, locations)
        return [
            AutoresponseLocation(*row) for row in cursor.execute(SELECT_BY_GUILD_STATEMENT, (locations[0].guild_id,))
        ]


def _clear(conn: apsw.Connection, guild_id: int) -> None:
    with conn:
        cursor = conn.cursor()
        cursor.execute(CLEAR_GUILD_CHANNELS_STATEMENT, (guild_id,))


class StoryWebsite(NamedTuple):
    name: str
    acronym: str
    story_regex: re.Pattern[str]
    icon_url: str


STORY_WEBSITE_STORE = {
    "FFN": StoryWebsite(
        "FanFiction.Net",
        "FFN",
        re.compile(r"(?:www\.|m\.|)fanfiction\.net/s/(?P<ffn_id>\d+)"),
        "https://www.fanfiction.net/static/icons3/ff-icon-128.png",
    ),
    "FP": StoryWebsite(
        "FictionPress",
        "FP",
        re.compile(r"(?:www\.|m\.|)fictionpress\.com/s/\d+"),
        "https://www.fanfiction.net/static/icons3/ff-icon-128.png",
    ),
    "AO3": StoryWebsite(
        "Archive of Our Own",
        "AO3",
        re.compile(r"(?:www\.|)archiveofourown\.org/(?P<type>works|series)/(?P<ao3_id>\d+)"),
        ao3.utils.AO3_LOGO_URL,
    ),
    "SB": StoryWebsite(
        "SpaceBattles",
        "SB",
        re.compile(r"forums\.spacebattles\.com/threads/\S*"),
        "https://forums.spacebattles.com/data/svg/2/1/1682578744/2022_favicon_192x192.png",
    ),
    "SV": StoryWebsite(
        "Sufficient Velocity",
        "SV",
        re.compile(r"forums\.sufficientvelocity\.com/threads/\S*"),
        "https://forums.sufficientvelocity.com/favicon-96x96.png?v=69wyvmQdJN",
    ),
    "QQ": StoryWebsite(
        "Questionable Questing",
        "QQ",
        re.compile(r"forums\.questionablequesting\.com/threads/\S*"),
        "https://forums.questionablequesting.com/favicon.ico",
    ),
    "SIYE": StoryWebsite(
        "Sink Into Your Eyes",
        "SIYE",
        re.compile(r"(?:www\.|)siye\.co\.uk/(?:siye/|)viewstory\.php\?sid=\d+"),
        "https://www.siye.co.uk/siye/favicon.ico",
    ),
}

STORY_WEBSITE_REGEX = re.compile(
    r"(?:http://|https://|)"
    + "|".join(f"(?P<{key}>{value.story_regex.pattern})" for key, value in STORY_WEBSITE_STORE.items()),
)


class NotFoundEmbed(discord.Embed):
    """An embed that represents a story that could not be found."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(
            title="No Results",
            description="No results found. You may need to edit your search.",
            timestamp=discord.utils.utcnow(),
        )


@functools.cache
def create_ao3_work_embed(work: ao3.Work) -> discord.Embed:
    """Create an embed that holds all the relevant metadata for an Archive of Our Own work.

    Only accepts :class:`ao3.Work` objects.
    """

    # Format the relevant information.
    if work.date_updated:
        updated = work.date_updated.strftime("%B %d, %Y") + (" (Complete)" if work.is_complete else "")
    else:
        updated = "Unknown"
    author_names = ", ".join(str(author.name) for author in work.authors)
    fandoms = textwrap.shorten(", ".join(work.fandoms), 100, placeholder="...")
    categories = textwrap.shorten(", ".join(work.categories), 100, placeholder="...")
    characters = textwrap.shorten(", ".join(work.characters), 100, placeholder="...")
    details = " • ".join((fandoms, categories, characters))
    stats_str = " • ".join(
        (
            f"**Comments:** {work.ncomments:,d}",
            f"**Kudos:** {work.nkudos:,d}",
            f"**Bookmarks:** {work.nbookmarks:,d}",
            f"**Hits:** {work.nhits:,d}",
        ),
    )

    # Add the info in the embed appropriately.
    author_url = f"https://archiveofourown.org/users/{work.authors[0].name}"
    ao3_embed = (
        discord.Embed(title=work.title, url=work.url, timestamp=discord.utils.utcnow())
        .set_author(name=author_names, url=author_url, icon_url=STORY_WEBSITE_STORE["AO3"].icon_url)
        .add_field(name="\N{SCROLL} Last Updated", value=f"{updated}")
        .add_field(name="\N{OPEN BOOK} Length", value=f"{work.nwords:,d} words in {work.nchapters} chapter(s)")
        .add_field(name=f"\N{BOOKMARK} Rating: {work.rating}", value=details, inline=False)
        .add_field(name="\N{BAR CHART} Stats", value=stats_str, inline=False)
        .set_footer(text="A substitute for displaying AO3 information.")
    )

    # Use the remaining space in the embed for the truncated description.
    ao3_embed.description = textwrap.shorten(work.summary, 6000 - len(ao3_embed), placeholder="...")
    return ao3_embed


@functools.cache
def create_ao3_series_embed(series: ao3.Series) -> discord.Embed:
    """Create an embed that holds all the relevant metadata for an Archive of Our Own series.

    Only accepts :class:`ao3.Series` objects.
    """

    author_url = f"https://archiveofourown.org/users/{series.creators[0].name}"

    # Format the relevant information.
    if series.date_updated:
        updated = series.date_updated.strftime("%B %d, %Y") + (" (Complete)" if series.is_complete else "")
    else:
        updated = "Unknown"
    author_names = ", ".join(name for creator in series.creators if (name := creator.name))
    work_links = "\N{BOOKS} **Works:**\n" + "\n".join(f"[{work.title}]({work.url})" for work in series.works_list)

    # Add the info in the embed appropriately.
    ao3_embed = (
        discord.Embed(title=series.name, url=series.url, description=work_links, timestamp=discord.utils.utcnow())
        .set_author(name=author_names, url=author_url, icon_url=STORY_WEBSITE_STORE["AO3"].icon_url)
        .add_field(name="\N{SCROLL} Last Updated", value=updated)
        .add_field(name="\N{OPEN BOOK} Length", value=f"{series.nwords:,d} words in {series.nworks} work(s)")
        .set_footer(text="A substitute for displaying AO3 information.")
    )

    # Use the remaining space in the embed for the truncated description.
    series_descr = textwrap.shorten(series.description + "\n\n", 6000 - len(ao3_embed), placeholder="...\n\n")
    ao3_embed.description = series_descr + (ao3_embed.description or "")
    return ao3_embed


@functools.cache
def create_atlas_ffn_embed(story: atlas_api.Story) -> discord.Embed:
    """Create an embed that holds all the relevant metadata for a FanFiction.Net story.

    Only accepts :class:`atlas_api.Story` objects.
    """

    # Format the relevant information.
    update_date = story.updated if story.updated else story.published
    updated = update_date.strftime("%B %d, %Y") + (" (Complete)" if story.is_complete else "")
    fandoms = textwrap.shorten(", ".join(story.fandoms), 100, placeholder="...")
    genres = textwrap.shorten("/".join(story.genres), 100, placeholder="...")
    characters = textwrap.shorten(", ".join(story.characters), 100, placeholder="...")
    details = " • ".join((fandoms, genres, characters))
    stats = f"**Reviews:** {story.reviews:,d} • **Faves:** {story.favorites:,d} • **Follows:** {story.follows:,d}"

    # Add the info to the embed appropriately.
    ffn_embed = (
        discord.Embed(title=story.title, url=story.url, description=story.description, timestamp=discord.utils.utcnow())
        .set_author(name=story.author.name, url=story.author.url, icon_url=STORY_WEBSITE_STORE["FFN"].icon_url)
        .add_field(name="\N{SCROLL} Last Updated", value=updated)
        .add_field(name="\N{OPEN BOOK} Length", value=f"{story.words:,d} words in {story.chapters} chapter(s)")
        .add_field(name=f"\N{BOOKMARK} Rating: Fiction {story.rating}", value=details, inline=False)
        .add_field(name="\N{BAR CHART} Stats", value=stats, inline=False)
        .set_footer(text="Made using iris's Atlas API. Some results may be out of date or unavailable.")
    )

    # Use the remaining space in the embed for the truncated description.
    ffn_embed.description = textwrap.shorten(story.description, 6000 - len(ffn_embed), placeholder="...")
    return ffn_embed


@functools.cache
def create_fichub_embed(story: fichub_api.Story) -> discord.Embed:
    """Create an embed that holds all the relevant metadata for a few different types of online fiction story.

    Only accepts :class:`fichub_api.Story` objects.
    """

    # Format the relevant information.
    updated = story.updated.strftime("%B %d, %Y")
    fandoms = textwrap.shorten(", ".join(story.fandoms), 100, placeholder="...")
    categories_list = story.more_meta.get("category", [])
    categories = textwrap.shorten(", ".join(categories_list), 100, placeholder="...")
    characters = textwrap.shorten(", ".join(story.characters), 100, placeholder="...")
    details = " • ".join((fandoms, categories, characters))

    # Get site-specific information, since FicHub works for multiple websites.
    icon_url = next(
        (value.icon_url for value in STORY_WEBSITE_STORE.values() if re.search(value.story_regex, story.url)),
        None,
    )

    if "fanfiction.net" in story.url:
        stats_names = ("reviews", "favorites", "follows")
        stats_str = " • ".join(f"**{name.capitalize()}:** {story.stats[name]:,d}" for name in stats_names)
    elif "archiveofourown.org" in story.url:
        stats_names = ("comments", "kudos", "bookmarks", "hits")
        # Account for absent extended metadata.
        stats = (
            f"**{stat_name.capitalize()}:** {ind_stat:,d}"
            for stat_name in stats_names
            if (ind_stat := story.stats.get(stat_name)) is not None
        )
        stats_str = " • ".join(stats)
    else:
        stats_str = "No stats available at this time."

    # Add the info to the embed appropriately.
    story_embed = (
        discord.Embed(title=story.title, url=story.url, description=story.description, timestamp=discord.utils.utcnow())
        .set_author(name=story.author.name, url=story.author.url, icon_url=icon_url)
        .add_field(name="\N{SCROLL} Last Updated", value=f"{updated} ({story.status.capitalize()})")
        .add_field(name="\N{OPEN BOOK} Length", value=f"{story.words:,d} words in {story.chapters} chapter(s)")
        .add_field(name=f"\N{BOOKMARK} Rating: {story.rating}", value=details, inline=False)
        .add_field(name="\N{BAR CHART} Stats", value=stats_str, inline=False)
        .set_footer(text="Made using the FicHub API. Some results may be out of date or unavailable.")
    )

    # Use the remaining space in the embed for the truncated description.
    story_embed.description = textwrap.shorten(story.description, 6000 - len(story_embed), placeholder="...")
    return story_embed


EMBED_STRATEGIES: dict[Any, Callable[..., discord.Embed]] = {
    atlas_api.Story: create_atlas_ffn_embed,
    fichub_api.Story: create_fichub_embed,
    ao3.Work: create_ao3_work_embed,
    ao3.Series: create_ao3_series_embed,
}


def ff_embed_factory(story_data: Any | None) -> discord.Embed:
    strategy = EMBED_STRATEGIES.get(type(story_data), NotFoundEmbed)
    return strategy(story_data)


class AO3SeriesView(discord.ui.View):
    """A view that wraps a AO3 works with a pagination view.

    Parameters
    ----------
    author_id: :class:`int`
        The Discord ID of the user that triggered this view. No one else can use it.
    series: :class:`ao3.Series`
        The object holding metadata about an AO3 series and the works within.
    timeout: :class:`float` | None, optional
        Timeout in seconds from last interaction with the UI before no longer accepting input.
        If ``None`` then there is no timeout.

    Attributes
    ----------
    message: :class:`discord.Message`
        The message to which the view is attached to, allowing interaction without a :class:`discord.Interaction`.
    author_id: :class:`int`
        The Discord ID of the user that triggered this view. No one else can use it.
    series: :class:`ao3.Series`
        The object holding metadata about an AO3 series and the works within.
    page_index: :class:`int`
        The index for the current page.
    total_pages
    """

    message: discord.Message

    def __init__(self, author_id: int, series: ao3.Series, *, timeout: float | None = 180) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.series = series
        self.page_index: int = 0

        self.populate_select()

        # Activate the right buttons on instantiation.
        self.disable_page_buttons()

    @property
    def total_pages(self) -> int:
        """:class:``int`: The total number of pages."""

        return len(self.series.works_list)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        """Allows to the interaction to be processed if the user interacting is the view owner."""

        check = self.author_id == interaction.user.id
        if not check:
            await interaction.response.send_message("You cannot interact with this view.", ephemeral=True)
        return check

    async def on_timeout(self) -> None:
        """Disables all items on timeout."""

        for item in self.children:
            item.disabled = True  # type: ignore

        await self.message.edit(view=self)
        self.stop()

    def format_page(self) -> discord.Embed:
        """Makes and returns the series/work 'page' that the user will see."""

        if self.page_index != 0:
            embed_page = create_ao3_work_embed(self.series.works_list[self.page_index - 1])
        else:
            embed_page = create_ao3_series_embed(self.series)
        return embed_page

    def populate_select(self) -> None:
        """Populates the select with relevant options."""

        self.select_page.placeholder = "Choose the work here..."
        descr = textwrap.shorten(self.series.description, 100, placeholder="...")
        self.select_page.add_option(label=self.series.name, value="0", description=descr, emoji="\N{BOOKS}")

        for i, work in enumerate(self.series.works_list, start=1):
            descr = textwrap.shorten(work.summary, 100, placeholder="...")
            self.select_page.add_option(
                label=f"{i}. {work.title}",
                value=str(i),
                description=descr,
                emoji="\N{OPEN BOOK}",
            )

    def disable_page_buttons(self) -> None:
        """Enables and disables page-turning buttons based on page count, position, and movement."""

        self.turn_to_previous.disabled = self.page_index == 0
        self.turn_to_next.disabled = self.page_index == self.total_pages - 1

    async def get_first_page(self) -> discord.Embed:
        """Get the embed of the first page."""

        temp = self.page_index
        self.page_index = 0
        embed = self.format_page()
        self.page_index = temp
        return embed

    async def update_page(self, interaction: discord.Interaction) -> None:
        """Update and display the view for the given page."""

        embed_page = self.format_page()
        self.disable_page_buttons()
        await interaction.response.edit_message(embed=embed_page, view=self)

    @discord.ui.select(cls=discord.ui.Select[Self])
    async def select_page(self, interaction: discord.Interaction, select: discord.ui.Select[Self]) -> None:
        """Dropdown that displays all the Patreon tiers and provides them as choices to navigate to."""

        self.page_index = int(select.values[0])
        await self.update_page(interaction)

    @discord.ui.button(label="<", style=discord.ButtonStyle.blurple)
    async def turn_to_previous(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Turns to the previous page of the view."""

        self.page_index -= 1
        await self.update_page(interaction)

    @discord.ui.button(label=">", style=discord.ButtonStyle.blurple)
    async def turn_to_next(self, interaction: discord.Interaction, _: discord.ui.Button[Self]) -> None:
        """Turns to the next page of the view."""

        self.page_index += 1
        await self.update_page(interaction)


class ChannelNotFound(discord.app_commands.TransformerError):
    """Exception raised when a string fails to be converted to a Discord channel."""


class GuildChannelListTransformer(discord.app_commands.Transformer):
    """A transformer that attempts to transform a string input into a list of channels.

    Notes
    -----
    This assumes the command is being invoked in a guild and also does not search all channels available to the bot for
    a match.

    Much of the implementation is copied from discord.py's GuildChannelConverter.
    """

    async def transform(self, itx: discord.Interaction, value: str) -> list[discord.abc.GuildChannel]:
        value_split = re.split(" ", value)
        results: list[discord.abc.GuildChannel] = []

        for potential_channel in value_split:
            try:
                results.append(self._resolve_channel(itx, potential_channel))
            except ChannelNotFound:
                pass
        return results

    def _resolve_channel(self, itx: discord.Interaction, argument: str) -> discord.abc.GuildChannel:
        match = re.match(r"([0-9]{15,20})$", argument) or re.match(r"<#([0-9]{15,20})>$", argument)
        result = None
        guild = itx.guild
        assert itx.guild

        if guild:
            if match is None:
                # not a mention
                result = discord.utils.get(guild.channels, name=argument)
            else:
                channel_id = int(match.group(1))
                # guild.get_channel returns an explicit union instead of the base class
                result = guild.get_channel(channel_id)

        if not isinstance(result, discord.abc.GuildChannel):
            raise ChannelNotFound(argument, discord.AppCommandOptionType.string, self)

        return result


GuildChannelList: TypeAlias = discord.app_commands.Transform[
    list[discord.abc.GuildChannel],
    GuildChannelListTransformer,
]


wf_autoresponse = discord.app_commands.Group(
    name="wf_autoresponse",
    description="Autoresponse-related commands for automatically responding to fanfiction links in certain channels.",
    default_permissions=discord.Permissions(administrator=True),
    guild_only=True,
)


@wf_autoresponse.command(name="get")
async def wf_autoresponse_get(itx: discord.Interaction[WebficSearcherBot]) -> None:
    """Display the channels in the server set to autorespond to webfiction links."""

    assert itx.guild_id  # Known at runtime.

    await itx.response.defer()
    active_channels = await itx.client.get_guild_autoresponse_channels(itx.guild_id)
    embed = discord.Embed(
        title="Autoresponse Channels for Fanfic Links",
        description="\n".join(f"<#{result.channel_id}>" for result in active_channels),
    )
    await itx.followup.send(embed=embed)


@wf_autoresponse.command(name="add")
async def wf_autoresponse_add(itx: discord.Interaction[WebficSearcherBot], channels: GuildChannelList) -> None:
    """Set the bot to listen for AO3/FFN/other site links posted in the given channels.

    If allowed, the bot will respond automatically with an informational embed.

    Parameters
    ----------
    itx: :class:`discord.Interaction`
        The interaction that triggered this command.
    channels: :class:`commands.Greedy`[:class:`discord.abc.GuildChannel`]
        A list of channels to add, separated by spaces.
    """

    assert itx.guild_id  # Known at runtime.

    await itx.response.defer()

    # Update the database.
    channels_to_add = [AutoresponseLocation(itx.guild_id, channel.id) for channel in channels]
    active_channels = await itx.client.add_autoresponse_channels(channels_to_add)

    embed = discord.Embed(
        title="Adjusted Autoresponse Channels for Fanfic Links",
        description="\n".join(f"<#{row.channel_id}>" for row in active_channels),
    )
    await itx.followup.send(embed=embed, ephemeral=True)


@wf_autoresponse.command(name="remove")
async def wf_autoresponse_remove(itx: discord.Interaction[WebficSearcherBot], channels: GuildChannelList) -> None:
    """Set the bot to not listen for AO3/FFN/other site links posted in the given channels.

    The bot will no longer automatically respond to links with information embeds.

    Parameters
    ----------
    itx: :class:`discord.Interaction`
        The interaction that triggered this command.
    channels: :class:`commands.Greedy`[:class:`discord.abc.GuildChannel`]
        A list of channels to remove, separated by spaces.
    """

    assert itx.guild_id  # Known at runtime.

    await itx.response.defer()

    # Update the database.
    channels_to_remove = [AutoresponseLocation(itx.guild_id, channel.id) for channel in channels]
    active_channels = await itx.client.drop_autoresponse_channels(channels_to_remove)

    embed = discord.Embed(
        title="Adjusted Autoresponse Channels for Webfiction Links",
        description="\n".join(f"<#{row.channel_id}>" for row in active_channels),
    )
    await itx.followup.send(embed=embed, ephemeral=True)


@wf_autoresponse.command(name="clear")
async def wf_autoresponse_clear(itx: discord.Interaction[WebficSearcherBot]) -> None:
    """Set the bot to not listen for AO3/FFN/other site links posted any guild channels.

    The bot will no longer automatically respond to links with information embeds.

    Parameters
    ----------
    itx: :class:`discord.Interaction`
        The interaction that triggered this command.
    """

    assert itx.guild_id  # Known at runtime.

    await itx.response.defer()

    # Update the database.
    _clear(itx.client.db_connection, itx.guild_id)

    embed = discord.Embed(title="Cleared Autoresponse Channels for Webfiction Links")
    await itx.followup.send(embed=embed, ephemeral=True)


@discord.app_commands.command()
async def wf_search(
    itx: discord.Interaction[WebficSearcherBot],
    platform: Literal["ao3", "ffn", "other"],
    name_or_url: str,
) -> None:
    """Search available platforms for a fic with a certain title or url. Note: Only urls are accepted for `other`.

    Parameters
    ----------
    itx: :class:`discord.Interaction`
        The invocation interaction.
    platform: Literal["ao3", "ffn", "other"]
        The platform to search.
    name_or_url: :class:`str`
        The search string for the story title, or the story url.
    """

    await itx.response.defer()

    if platform == "ao3":
        story_data = await itx.client.search_ao3(name_or_url)
    elif platform == "ffn":
        story_data = await itx.client.search_ffn(name_or_url)
    else:
        story_data = await itx.client.search_other(name_or_url)

    embed = ff_embed_factory(story_data)

    if isinstance(story_data, ao3.Series):
        view = AO3SeriesView(itx.user.id, story_data)
        view.message = await itx.followup.send(embed=embed, view=view, wait=True)
    else:
        await itx.followup.send(embed=embed)


@discord.app_commands.command()
async def invite(itx: discord.Interaction[WebficSearcherBot]) -> None:
    """Get a link to invite this bot to a server."""

    embed = discord.Embed(description="Click the link below to invite me to one of your servers.")
    view = discord.ui.View().add_item(discord.ui.Button(label="Invite", url=itx.client.invite_link))
    await itx.response.send_message(embed=embed, view=view, ephemeral=True)


APP_COMMANDS = (wf_autoresponse, wf_search, invite)


def resolve_path_with_links(path: Path, folder: bool = False) -> Path:
    """Resolve a path strictly with more secure default permissions, creating the path if necessary.

    Python only resolves with strict=True if the path exists.

    Source: https://github.com/mikeshardmind/discord-rolebot/blob/4374149bc75d5a0768d219101b4dc7bff3b9e38e/rolebot.py#L350
    """

    try:
        return path.resolve(strict=True)
    except FileNotFoundError:
        path = resolve_path_with_links(path.parent, folder=True) / path.name
        if folder:
            path.mkdir(mode=0o700)  # python's default is world read/write/traversable... (0o777)
        else:
            path.touch(mode=0o600)  # python's default is world read/writable... (0o666)
        return path.resolve(strict=True)


class VersionableTree(discord.app_commands.CommandTree):
    """A command tree with a two new methods:

    1. Generate a unique hash to represent all commands currently in the tree.
    2. Compare hash of the current tree against that of a previous version using the above method.

    Credit to @mikeshardmind: Everything in this class is his.

    Notes
    -----
    The main use case is autosyncing using the hash comparison as a condition.
    """

    async def get_hash(self: Self) -> bytes:
        commands = sorted(self._get_all_commands(guild=None), key=lambda c: c.qualified_name)

        translator = self.translator
        if translator:
            payload = [await command.get_translated_payload(translator) for command in commands]
        else:
            payload = [command.to_dict() for command in commands]

        return xxhash.xxh3_64_digest(json.dumps(payload).encode("utf-8"), seed=1)

    async def sync_if_commands_updated(self: Self) -> None:
        """Sync the tree globally if its commands are different from the tree's most recent previous version.

        Comparison is done with hashes, with the hash being stored in a specific file if unique for later comparison.

        Notes
        -----
        This uses blocking file IO, so don't run this in situations where that matters. `setup_hook()` should be fine
        a fine place though.
        """

        tree_hash = await self.get_hash()
        tree_hash_path = platformdir_info.user_cache_path / "webfic_searcher_bot_tree.hash"
        tree_hash_path = resolve_path_with_links(tree_hash_path)
        with tree_hash_path.open("r+b") as fp:
            data = fp.read()
            if data != tree_hash:
                log.info("New version of the command tree. Syncing now.")
                await self.sync()
                fp.seek(0)
                fp.write(tree_hash)


class WebficSearcherBot(discord.AutoShardedClient):
    def __init__(self, *, session: aiohttp.ClientSession, atlas_auth: aiohttp.BasicAuth) -> None:
        super().__init__(
            intents=discord.Intents(guilds=True, messages=True, message_content=True),
            activity=discord.Game(name="https://github.com/Sachaa-Thanasius/discord-webfic-searcher"),
        )
        self.tree = VersionableTree(self)

        # Initialize the various API clients that are responsible for retrieving fic information.
        self._session = session
        self.ao3_client = ao3.Client(session=session)
        self.atlas_client = atlas_api.Client(auth=atlas_auth, session=session)
        self.fichub_client = fichub_api.Client(session=session)

        # Connect to the database that will store the radio information.
        # -- Need to account for the directories and/or file not existing.
        db_path = platformdir_info.user_data_path / "webfic_searcher_data.db"
        resolved_path_as_str = str(resolve_path_with_links(db_path))
        self.db_connection = apsw.Connection(resolved_path_as_str)

    async def on_connect(self: Self) -> None:
        """(Re)set the client's general invite link every time it (re)connects to the Discord Gateway."""

        await self.wait_until_ready()
        data = await self.application_info()
        perms = discord.Permissions(19456)  # TODO: Evaluate necessary permissions.
        self.invite_link = discord.utils.oauth_url(data.id, permissions=perms)

    async def setup_hook(self) -> None:
        # Initialize the database and start the loop.
        await asyncio.to_thread(_setup_db, self.db_connection)

        # Add the app commands to the tree.
        for cmd in APP_COMMANDS:
            self.tree.add_command(cmd)

        # Sync the tree if it's different from the previous version, using hashing for comparison.
        await self.tree.sync_if_commands_updated()

    async def on_message(self, message: discord.Message) -> None:
        """Send informational embeds about a story if the user sends a fanfiction link.

        Must be triggered in an allowed channel.
        """

        if (message.author == self.user) or (not message.guild):
            return

        # Listen to the allowed channels in the allowed guilds for valid fanfic links.
        if (
            (channels_cache := await self.get_guild_autoresponse_channels(message.guild.id))
            and ((message.guild.id, message.channel.id) in channels_cache)
            and re.search(STORY_WEBSITE_REGEX, message.content)
        ):
            # Only show typing indicator on valid messages.
            async with message.channel.typing():
                # Send an embed for every valid link.
                async for story_data in self.get_ff_data_from_links(message.content):
                    if story_data is not None:
                        embed = ff_embed_factory(story_data)
                        if not isinstance(embed, NotFoundEmbed):
                            await message.channel.send(embed=embed)

    async def get_all_autoresponse_channels(self) -> list[AutoresponseLocation]:
        return _query(self.db_connection, SELECT_ALL_STATEMENT)

    async def get_guild_autoresponse_channels(self, guild_id: int) -> list[AutoresponseLocation]:
        return _query(self.db_connection, SELECT_BY_GUILD_STATEMENT, (guild_id,))

    async def add_autoresponse_channels(self, locations: Sequence[AutoresponseLocation]) -> list[AutoresponseLocation]:
        return _add(self.db_connection, locations)

    async def drop_autoresponse_channels(self, locations: Sequence[AutoresponseLocation]) -> list[AutoresponseLocation]:
        return _drop(self.db_connection, locations)

    async def search_ao3(self, name_or_url: str) -> ao3.Work | ao3.Series | fichub_api.Story | None:
        """More generically search AO3 for works based on a partial title or full url."""

        if match := re.search(STORY_WEBSITE_STORE["AO3"].story_regex, name_or_url):
            if match.group("type") == "series":
                try:
                    series_id = match.group("ao3_id")
                    story_data = await self.ao3_client.get_series(int(series_id))
                except ao3.AO3Exception:
                    log.exception("")
                    story_data = None
            else:
                try:
                    url = match.group(0)
                    story_data = await self.fichub_client.get_story_metadata(url)
                except fichub_api.FicHubException as err:
                    msg = "Retrieval with Fichub client failed. Trying the AO3 scraping library now."
                    log.warning(msg, exc_info=err)
                    try:
                        work_id = match.group("ao3_id")
                        story_data = await self.ao3_client.get_work(int(work_id))
                    except ao3.AO3Exception as err:
                        msg = "Retrieval with Fichub client and AO3 scraping library failed. Returning None."
                        log.warning(msg, exc_info=err)
                        story_data = None
        else:
            search_options = ao3.WorkSearchOptions(any_field=name_or_url)
            search = await self.ao3_client.search_works(search_options)
            story_data = results[0] if (results := search.results) else None

        return story_data

    async def search_ffn(self, name_or_url: str) -> atlas_api.Story | fichub_api.Story | None:
        """More generically search FFN for works based on a partial title or full url."""

        if fic_id := atlas_api.extract_fic_id(name_or_url):
            try:
                story_data = await self.atlas_client.get_story_metadata(fic_id)
            except atlas_api.AtlasException as err:
                msg = "Retrieval with Atlas client failed. Trying FicHub now."
                log.warning(msg, exc_info=err)
                try:
                    story_data = await self.fichub_client.get_story_metadata(name_or_url)
                except fichub_api.FicHubException as err:
                    msg = "Retrieval with Atlas and Fichub clients failed. Returning None."
                    log.warning(msg, exc_info=err)
                    story_data = None
        else:
            results = await self.atlas_client.get_bulk_metadata(title_ilike=f"%{name_or_url}%", limit=1)
            story_data = results[0] if results else None

        return story_data

    async def search_other(self, url: str) -> fichub_api.Story | None:
        """More generically search for the metadata of other works based on a full url."""

        return await self.fichub_client.get_story_metadata(url)

    async def get_ff_data_from_links(self, text: str) -> AsyncGenerator[StoryDataType | None, None]:
        for match_obj in re.finditer(STORY_WEBSITE_REGEX, text):
            # Attempt to get the story data from whatever method.
            if match_obj.lastgroup == "FFN":
                story_data = await self.atlas_client.get_story_metadata(int(match_obj.group("ffn_id")))
            elif match_obj.lastgroup == "AO3":
                story_data = await self.search_ao3(match_obj.group(0))
            elif match_obj.lastgroup is not None:
                story_data = await self.search_other(match_obj.group(0))
            else:
                story_data = None
            yield story_data


def load_config() -> dict[str, Any]:
    config_path = Path("config.toml")
    with config_path.open(mode="rb", encoding="utf-8") as fp:
        return tomllib.load(fp)


def main() -> None:
    config = load_config()
    token = config["discord"]["token"]
    atlas_auth = aiohttp.BasicAuth(config["atlas"]["login"], config["atlas"]["password"])

    async def bot_runner() -> None:
        async with aiohttp.ClientSession() as session, WebficSearcherBot(
            session=session,
            atlas_auth=atlas_auth,
        ) as client:
            await client.start(token, reconnect=True)

    loop = uvloop.new_event_loop if (uvloop is not None) else None  # type: ignore
    with asyncio.Runner(loop_factory=loop) as runner:  # type: ignore
        runner.run(bot_runner())


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
