"""Explicit public-Domo-API projection contracts for Step 1, BMAT, and Monday.

These contracts replace card-export scraping.  Every deliverable column is
ordered and named here, so edits to a Domo card cannot silently change a file
consumed by the workflow.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProjectionField:
    output: str
    source: str | None = None
    transform: str | None = None
    constant: str | None = None


@dataclass(frozen=True)
class DomoProjectionContract:
    dataset_id: str
    fields: tuple[ProjectionField, ...]
    date_column: str | None = None
    unique_by: tuple[str, ...] = ()
    where_sql: str | None = None
    distinct: bool = False


def _direct(*names: str) -> tuple[ProjectionField, ...]:
    return tuple(ProjectionField(name, name) for name in names)


def _mapped(*pairs: tuple[str, str]) -> tuple[ProjectionField, ...]:
    return tuple(ProjectionField(output, source) for source, output in pairs)


US_TRACKLIST = (
    ProjectionField("Label", "LabelName"),
    ProjectionField("AlbumNo", "AlbumNo"),
    ProjectionField("AlbumTitle", "AlbumTitle"),
    ProjectionField("AlbumNoMasters", transform="album_no_masters"),
    ProjectionField("ReleaseDate", "AlbumReleaseDate", "date"),
    ProjectionField("WorkTitle", "WorkTitle"),
    ProjectionField("TrackNo", "TrackNo"),
    ProjectionField("workAudioId", "domoAudioId"),
    ProjectionField("Filename", "AudioFile", "filename_no_extension"),
    ProjectionField("PipsCode", "PipsCode"),
    ProjectionField("ComposerNames", "ComposerNames"),
    ProjectionField("AlbumCoverArt", "AlbumCoverArt", "clean_cover"),
    ProjectionField("CDNAlbumArt", "AlbumCoverArt", "cdn_album_art"),
)

EXUS_TRACKLIST = (
    ProjectionField("Label", "LabelName"),
    ProjectionField("AlbumNo", "AlbumNo"),
    ProjectionField("AlbumTitle", "AlbumTitle"),
    ProjectionField("ReleaseDate", "AlbumReleaseDate", "date"),
    ProjectionField("WorkTitle", "WorkTitle"),
    ProjectionField("TrackNo", "TrackNo"),
    ProjectionField("workAudioId", "domoAudioId"),
    ProjectionField("Filename", "AudioFile", "filename_no_extension"),
    ProjectionField("AlbumCoverArt", "AlbumCoverArt", "clean_cover"),
    ProjectionField("CDNAlbumArt", "AlbumCoverArt", "cdn_album_art"),
)

JAPAN_METADATA = (
    ProjectionField("TRACK TITLE", "WorkTitle"),
    ProjectionField("TRACK NUMBER", "TrackNo"),
    ProjectionField("ALBUM TITLE", "AlbumTitle"),
    ProjectionField("CATALOG NUMBER", "AlbumNo"),
    ProjectionField("LABEL", "LabelName"),
    ProjectionField("AUTHOR", "Authors"),
    ProjectionField("COMPOSER", "ComposerNamesAndSocieties"),
    ProjectionField("ARTIST", "Brand"),
    ProjectionField("ARRANGER", "Arrangers"),
    ProjectionField("TRANSLATOR", constant=""),
    ProjectionField("JASRAC CODE", constant=""),
    ProjectionField("OriginalPublisher", "PublisherNamesAndSocieties"),
    ProjectionField("SUBPUBLISHER", constant=""),
    ProjectionField("Filename", "AudioFile"),
    ProjectionField("workAudioId", "domoAudioId"),
)

TUNESAT_METADATA = (
    ProjectionField("Song Code", "workAudioId"),
    ProjectionField("File Name", "Filename", "mp3_filename"),
    ProjectionField("Track Title", "TrackTitle"),
    ProjectionField("Album", "CDTitle"),
    ProjectionField("Labels", "Library"),
    ProjectionField("Composers", "Composer", "tunesat_people"),
    ProjectionField("Publisher", "Publisher", "tunesat_people"),
    ProjectionField("ISRC", "ISRC"),
    ProjectionField("Album Code", "AlbumNo"),
    ProjectionField("Track Number", transform="album_track_number"),
)

NETMIX_COLUMNS = (
    "Filename", "Agent", "Label", "Labelcode", "LibraryCDno",
    "LibraryCDtitle", "Disc", "LibraryCDdescription", "Track",
    "Mainversion", "TracksperTitle", "Trackversion", "Title",
    "Description", "Category", "Music_Styles", "Moods", "Music_For",
    "Instruments", "Tempos", "TempoBPM", "Showtitle", "Lyrics",
    "Well_Known_tunes", "Eras", "Dances", "Countries", "Releasedate",
    "Trackid", "Composer1", "ComposerAffiliation1", "ComposerSharePerc1",
    "Composer2", "ComposerAffiliation2", "ComposerSharePerc2", "Composer3",
    "ComposerAffiliation3", "ComposerSharePerc3", "Composer4",
    "ComposerAffiliation4", "ComposerSharePerc4", "Composer5",
    "ComposerAffiliation5", "ComposerSharePerc5", "Composer6",
    "ComposerAffiliation6", "ComposerSharePerc6", "Composer7",
    "ComposerAffiliation7", "ComposerSharePerc7", "Composer8",
    "ComposerAffiliation8", "ComposerSharePerc8", "Composer9",
    "ComposerAffiliation9", "ComposerSharePerc9", "Publisher1",
    "PublisherAffiliation1", "PublisherSharePerc1", "Publisher2",
    "PublisherAffiliation2", "PublisherSharePerc2", "Publisher3",
    "PublisherAffiliation3", "PublisherSharePerc3", "Publisher4",
    "PublisherAffiliation4", "PublisherSharePerc4",
)

SYNCHTANK_COLUMNS = tuple(
    item
    for number in range(1, 13)
    for item in (
        f"COMPOSER {number} NAME", f"COMPOSER {number} PRO",
        f"COMPOSER {number} IPI", f"COMPOSER {number} SPLIT",
    )
) + tuple(
    item
    for number in range(1, 11)
    for item in (
        f"PUBLISHER {number} NAME", f"PUBLISHER {number} PRO",
        f"PUBLISHER {number} IPI", f"PUBLISHER {number} SPLIT",
    )
) + (
    "Label", "Album Code", "Album", "Album Release Date",
    "Album Description", "Track ID", "Title", "Track Description",
    "Track Number", "Track Type", "Main Track ID", "Version Type",
    "Length", "Lyrics", "Explicit", "Tempo", "BPM", "Genre", "Moods",
    "Styles", "Style Of", "Featured Instruments", "Music For",
    "Country List", "Eras", "CLINE", "PLINE", "ISRC", "ISWC",
    "Filename", "Album Cover Art",
)

SCRIPPS_COLUMNS = (
    "workAudioId", "Filename", "TrackTitle", "Version", "LabelAcronym",
    "Library", "AlbumNo", "CDTitle", "Category", "CDDescription",
    "Album Release Date", "Description", "BWDescription", "Duration",
    "Mood", "Notes", "FeaturedInstrument", "BPM", "Manufacturer",
    "Publisher", "Composer", "Source", "Lyrics", "Is_Explicit",
    "HasVocals", "is_SongBasedonLyrics", "EditType", "Catalog",
    "ComposerNamesAndSocieties", "PublisherNamesAndSocieties", "TrackNo",
    "ISRC",
)

QWIRE_COLUMNS = (
    "libraryName", "libraryShortName", "catalogNumber", "albumCode",
    "albumName", "albumDescription", "libraryTrackId", "trackTitle",
    "trackDescription", "TrackYear", "trackNumber", "trackIndex",
    "duration", "iswc", "isrc",
) + tuple(
    item
    for number in range(1, 9)
    for item in (
        f"comp.{number}.firstName", f"comp.{number}.lastName",
        f"comp.{number}.affiliation", f"comp.{number}.ipi",
        f"comp.{number}.share", f"comp.{number}.role",
    )
) + tuple(
    item
    for number in range(1, 6)
    for item in (
        f"pub.{number}.name", f"pub.{number}.affiliation",
        f"pub.{number}.ipi", f"pub.{number}.share", f"pub.{number}.role",
    )
)


def _sourceaudio_fields(*, exus: bool) -> tuple[ProjectionField, ...]:
    has_vocals = "Has Vocals" if exus else "HasVocals"
    fields = [
        ProjectionField("Title", "WorkTitle"),
        ProjectionField("External Id", "domoAudioId"),
        ProjectionField("Version", transform="sourceaudio_version"),
        ProjectionField("VersionID", "DomoVersionId"),
        ProjectionField("Filename", "Filename"),
        ProjectionField("Catalog", constant=(
            "Universal Production Music - Ex-US" if exus
            else "Universal Production Music - US"
        )),
        ProjectionField("Label", "LabelName"),
        ProjectionField("Album", "AlbumTitle"),
        ProjectionField("Album Code", "AlbumNo"),
        ProjectionField("Album Description", "AlbumDescription"),
        ProjectionField("Album Release Date", "AlbumReleaseDate", "date"),
        ProjectionField("Album Artist", "ComposerNames"),
        ProjectionField("Album Genre", "MainAlbumTagGenres"),
        ProjectionField("Track Number", "TrackNo"),
        ProjectionField("Artist", "ComposerNamesAndSocieties"),
        ProjectionField("Artist Description", constant=""),
        ProjectionField("Tempo", "VersionTagTempos"),
        ProjectionField("BPM", "BPM"),
        ProjectionField("Key", constant=""),
        ProjectionField("Genre", "MainVersionTagGenres"),
        ProjectionField("Cue Type", constant="Songs"),
        ProjectionField("Release Date", "AlbumReleaseDate", "date"),
        ProjectionField("Private", constant=""),
        ProjectionField("Length", "Duration", "duration"),
        ProjectionField("Description", "WorkDescription"),
        ProjectionField("Lyrics", "Lyrics"),
        ProjectionField(has_vocals, "HasVocals"),
        ProjectionField("Explicit", "Explicit"),
        ProjectionField("Keywords", transform="sourceaudio_keywords"),
        ProjectionField("Style", "MainVersionTagGenres"),
        ProjectionField("Mood", "MainVersionTagMoods"),
        ProjectionField("Music For", "MainVersionTagMusicFor"),
        ProjectionField("Instruments", "MainVersionTagInstruments"),
        ProjectionField("Country List", "MainVersionTagCountries"),
        ProjectionField("Eras", "VersionTagEras"),
        ProjectionField("ISRC", "ISRC"),
        ProjectionField("ISWC", "ISWC"),
        ProjectionField("Master Filename", "Master Filename"),
        ProjectionField("Nesting Sort Position", "NestingSortPosition"),
    ]
    for number in range(1, 11):
        fields.extend((
            ProjectionField(f"Publisher {number} Company", f"PublisherName_{number}"),
            ProjectionField(f"Publisher {number} Role", f"PublisherName_{number}", "publisher_role"),
            ProjectionField(f"Publisher {number} Pro Affiliation", f"PublisherSociety_{number}"),
            ProjectionField(f"Publisher {number} Ownership Share", f"PublisherSplit_{number}"),
        ))
    for number in range(1, 16):
        fields.extend((
            ProjectionField(f"Writer {number} First Name", f"ComposerForename_{number}"),
            ProjectionField(f"Writer {number} Last Name", f"ComposerSurname_{number}"),
            ProjectionField(f"Writer {number} Pro Affiliation", f"ComposerSociety_{number}"),
            ProjectionField(f"Writer {number} CAE/IPI", f"ComposerCAECode_{number}"),
            ProjectionField(f"Writer {number} Ownership Share", f"ComposerSplit_{number}"),
            ProjectionField(f"Writer {number} Role", f"ComposerType_{number}"),
        ))
    fields.extend((
        ProjectionField("Album Cover Art", "AlbumCoverArt"),
        ProjectionField("TrackCode", "TrackCode"),
    ))
    return tuple(fields)


JMD_TSS_COLUMNS = (
    "WorkGroupingId", "LabelAcronym", "LabelName", "AlbumNo", "AlbumTitle",
    "AlbumReleaseDate", "AlbumDescription", "WorkTitle", "WorkDescription",
    "VersionType", "VersionDescription", "TrackNo", "EditType",
    "DurationTime", "ComposerNamesAndSocieties", "ComposerSplits",
    "ComposerSocieties", "ComposerCAECodes", "ComposerTypes",
    "PublisherNamesAndSocieties", "PublisherSplits", "PublisherSocieties",
    "AudioFile", "ISRC", "ISWC", "PipsCode", "BPM",
)

SOUNDEXCHANGE_COLUMNS = (
    "Artist", "Recording Title", "ISRC",
    "What is the basis of your claim? (Copyright Owner or Collections Designee)",
    "Percentage Claimed", "Collection Rights Begin Date",
    "Collection Rights End Date", "Non-US Territories of Collection Rights",
    "Recording Version", "Duration", "Genre", "Recording Date",
    "Country of Recording/Fixation", "Country of Mastering",
    "Copyright Owner Country of Nationality", "Date of First Release",
    "Country/Countries of First Release/Publication", "(P) Line", "ISWC",
    "Composer(s)", "Publisher(s)", "Release Artist",
    "Release Title (Album Title)", "Release Version", "UPC", "Catalog #",
    "Release Date", "Country of Release", "Release Label",
)

BMAT_SUBMISSION_COLUMNS = (
    "TrackFilepath", "TrackDisplayTitle", "Library", "SubLibrary",
    "InternalID", "CatNo", "CDTitle", "TrackNo", "TrackSubNo",
    "TrackTitle", "TrackAlternateTitle", "Mixout", "Version", "Length",
    "GEN:1:Genre", "GEN:1:SubGenre", "GEN:2:Genre", "GEN:2:SubGenre",
    "GEN:3:Genre", "GEN:3:SubGenre", "BPM", "Tempo", "Mood", "Keywords",
    "Instrumentation", "TrackDescription", "CDDescription", "ReleaseDate",
    "Lyrics",
) + tuple(
    item
    for number in range(1, 9)
    for item in (
        f"COM:{number}:NamesBeforeKeyName", f"COM:{number}:KeyName",
        f"COM:{number}:Society", f"COM:{number}:IPI",
        f"COM:{number}:PerformanceShare",
    )
) + (
    "ARR:1:NamesBeforeKeyName", "ARR:1:KeyName", "ARR:1:Society",
    "ARR:1:IPI", "ARR:1:PerformanceShare",
) + tuple(
    item
    for number in range(1, 5)
    for item in (
        f"PUB:{number}:KeyName", f"PUB:{number}:Society",
        f"PUB:{number}:IPI", f"PUB:{number}:PerformanceShare",
    )
) + (
    "CODE:ISRC", "CODE:ISWC", "CODE:PREF", "CODE:PRSTuneCode",
    "CODE:GEMA", "CODE:SACEM", "CODE:SUISA", "CODE:UPC",
    "CODE:BUMASTEMRA", "CODE:SABAM", "CODE:APRA", "CODE:SGAE",
    "CODE:SGAE MUSIC USAGE", "CODE:EAN", "CODE:ASCAP", "CODE:BMI",
    "CODE:IMRO", "CODE:JASRAC", "CODE:KODA", "CODE:KOMCA", "CODE:NORM",
    "CODE:SAMRO", "CODE:SESAC", "CODE:SIAE", "CODE:SOCAN", "CODE:STIM",
    "CODE:TONO", "CODE:TEOSTO", "ATT:Artistname", "ATT:AlbumArtistname",
    "ATT:AlbumDiscs", "ATT:AlbumDiscNumber", "CDArtwork",
)


def _bmat_submission_fields() -> tuple[ProjectionField, ...]:
    scrubbed = {
        "TrackDisplayTitle", "InternalID", "ARR:1:IPI", "PUB:1:IPI",
        *(f"COM:{n}:IPI" for n in range(1, 9)),
    }
    return tuple(
        ProjectionField(output, output, "text" if output in scrubbed else None)
        for output in BMAT_SUBMISSION_COLUMNS
    )


DOMO_PROJECTION_CONTRACTS = {
    "monday_audio_batch": DomoProjectionContract(
        "2e2c7f18-c9d1-45cf-897c-be18939cc044",
        (
            ProjectionField("WorkGroupingId", "DomoAlbumId"),
            # The workflow's exact-date key is authoritative.  Leaving Batch
            # blank lets monday_sync bind every validated row to that key;
            # the source DataSet's legacy semi-monthly Workflow ID does not
            # describe rolling Saturday-through-Friday releases.
            ProjectionField("Batch", constant=""),
            ProjectionField("Catalog", "Catalog"),
            ProjectionField("Release Date", "AlbumReleaseDate", "date"),
            ProjectionField("LabelId", "LabelId"),
            ProjectionField("Album Code", "AlbumNo"),
            ProjectionField("Album Title", "AlbumTitle"),
            ProjectionField("Digital Fulfillment", constant="Create NEW"),
            # monday_sync assigns one deterministic trigger after validating
            # and de-duplicating the complete projected batch.
            ProjectionField("Batch Master", constant=""),
        ),
        "AlbumReleaseDate", ("WorkGroupingId",), distinct=True,
    ),
    "us_tracklist": DomoProjectionContract(
        "5768120c-002a-48d6-a5c5-58988d39d701", US_TRACKLIST,
        "AlbumReleaseDate", ("Filename", "workAudioId"),
    ),
    "exus_tracklist": DomoProjectionContract(
        "c20dc03b-9e25-4fda-aaf3-c48f39f87a6b", EXUS_TRACKLIST,
        "AlbumReleaseDate", ("Filename", "workAudioId"),
    ),
    "album_list": DomoProjectionContract(
        "e5552bdf-69a9-486f-9499-dc4c68a90fcf",
        _direct("Label", "Album Code", "Album Title"), "AlbumReleaseDate",
        ("Album Code",),
    ),
    "japan_metadata": DomoProjectionContract(
        "c3dacc3f-771b-4b2f-9925-6d51029c5f8d", JAPAN_METADATA,
        "Album Release Date", ("Filename", "workAudioId"),
    ),
    "tunesat_metadata": DomoProjectionContract(
        "e4d2207c-741d-4cde-bb03-7a2b733dabc0", TUNESAT_METADATA,
        "Album Release Date", ("File Name", "Song Code"),
    ),
    "netmix_metadata": DomoProjectionContract(
        "23f375d2-06ec-48f6-ab85-b73dee4f6c8d", _direct(*NETMIX_COLUMNS),
        "Releasedate", ("Filename", "Trackid"),
    ),
    "synchtank_metadata": DomoProjectionContract(
        "4c4f748b-118e-45da-b643-89d47c3feb90",
        (ProjectionField("Manufacturer", constant="Universal Production Music"),)
        + _direct(*SYNCHTANK_COLUMNS),
        "Album Release Date", ("Filename", "Track ID"),
    ),
    "scripps_metadata": DomoProjectionContract(
        "e4d2207c-741d-4cde-bb03-7a2b733dabc0", _direct(*SCRIPPS_COLUMNS),
        "Album Release Date", ("Filename", "workAudioId"),
    ),
    "qwire_metadata": DomoProjectionContract(
        "c0744caf-06c0-4ebf-bf65-e402cc30c4a3",
        tuple(
            ProjectionField(name, "AlbumReleaseDate", "year")
            if name == "TrackYear" else ProjectionField(name, name)
            for name in QWIRE_COLUMNS
        ),
        "AlbumReleaseDate", ("libraryTrackId", "isrc"),
    ),
    "sourceaudio_metadata": DomoProjectionContract(
        "261558d1-a21d-46a7-9d5b-a9c15bce1e9e",
        _sourceaudio_fields(exus=False), "AlbumReleaseDate",
        ("Filename", "External Id"),
    ),
    "sourceaudio_exus_metadata": DomoProjectionContract(
        "c88dc038-8891-486d-abea-8eada186ffea",
        _sourceaudio_fields(exus=True), "AlbumReleaseDate",
        ("Filename", "External Id"),
    ),
    "japan_jmdtss_metadata": DomoProjectionContract(
        "c3dacc3f-771b-4b2f-9925-6d51029c5f8d",
        tuple(
            ProjectionField(name, "DomoAlbumId" if name == "WorkGroupingId" else (
                "Releasedate" if name == "AlbumReleaseDate" else name
            )) for name in JMD_TSS_COLUMNS
        ),
        "Album Release Date", ("AudioFile", "WorkGroupingId"),
    ),
    "soundexchange_mgb": DomoProjectionContract(
        "b7df2efe-2519-4c9a-b9e0-97c8fb517f50",
        _direct(*SOUNDEXCHANGE_COLUMNS), "Release Date", ("ISRC",),
    ),
    "soundexchange_ztunes": DomoProjectionContract(
        "52671f9c-6cd0-4522-9d37-154cb95ef833",
        _direct(*SOUNDEXCHANGE_COLUMNS), "Release Date", ("ISRC",),
    ),
    "bmat_releases": DomoProjectionContract(
        "4a42f879-f694-4718-b875-6a095bfea436",
        _direct("AlbumNo", "AlbumTitle", "WorkTitle", "LabelName", "PublishingStatusDt"),
        "PublishingStatusDt", where_sql="`LabelName` = 'ELIAS CUSTOM'",
        distinct=True,
    ),
    "bmat_submission": DomoProjectionContract(
        "7d23ccd2-2171-4566-8e32-b7905f7dafe0",
        _bmat_submission_fields(), None, ("TrackFilepath", "InternalID"),
        "`Library` IN ('As Heard On TV', 'ELIAS CUSTOM')",
    ),
}
