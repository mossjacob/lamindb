import base64
import hashlib
from functools import partial
from pathlib import Path
from typing import Any, Dict, Optional

import lnschema_core as core
import sqlmodel as sqm
from lndb_setup import settings

from ...schema._table import table_meta
from .._core import get_name_suffix_from_filepath
from ..file import load_to_memory, store_file, write_adata_zarr
from ..file._file import print_hook
from ..object import infer_suffix, size_adata, write_to_file
from ._add import add
from ._core import get_foreign_keys, get_link_table
from ._link_knowledge import LinkFeatureToKnowledgeTable
from ._select import select
from ._track_usage import track_usage


class Staged:
    """Staged data objects, initiated upon :meth:`~lamindb.Ingest.add`.

    Guide: :doc:`/db/guide/ingest`.

    Args:
        data: filepath or in-memory objects
        dtransform: The data transform that links to the data source of the data object.
        name: name of the data object, required of an in-memory object is passed
        dobject_id: id of the dobject
        adata_format: Use `h5ad` or `zarr` to store an `AnnData` object
    """

    def __init__(
        self,
        data: Any,
        *,
        dtransform: core.dtransform,
        name: Optional[str] = None,
        dobject_id: Optional[str] = None,
        adata_format: Optional[str] = None,
    ) -> None:
        self._data = data  # input data object provided by user
        self._dmem = None  # in-memory object
        if isinstance(data, (Path, str)):
            # if a file is given, create a dobject
            # TODO: handle compressed files
            self._filepath = Path(data)
            name, suffix = get_name_suffix_from_filepath(self._filepath)
        else:
            # if in-memory object is given, return the cache path
            # if zarr, then filename is just name.zarr
            self._dmem = data
            suffix = infer_suffix(data, adata_format)
            if name is None:
                raise RuntimeError("Provide name if ingesting in-memory data.")
            self._filepath = Path(f"{name}{suffix}")
            # write to file
            # TODO: should this raise an error/warning if filepath exists?
            if suffix != ".zarr" and not self._filepath.exists():
                write_to_file(self._dmem, self._filepath)  # type: ignore

        self._dobject = core.dobject(name=name, suffix=suffix)
        self._dobject.id = dobject_id if dobject_id is not None else self.dobject.id
        # streamed
        if suffix != ".zarr":
            checksum = compute_checksum(self._filepath)
            result = (
                select(core.dobject)
                .where(core.dobject.checksum == checksum)
                .one_or_none()
            )
            if result is not None:
                raise RuntimeError(
                    "Based on the MD5 checksum, the exact same data object is already"
                    f" in the database with metadata: {result}"
                )
            self._dobject.checksum = checksum

        # access to the knowledge table
        self._knowledge_table = None

        # dtransform
        self._dtransform = dtransform

        # annotation entries
        self._entries: Dict = {}  # staged entries to be added

    @property
    def dobject(self) -> core.dobject:
        """An dobject entry to be added."""
        return self._dobject

    def _add_entry(self, entry: sqm.SQLModel) -> None:
        """Add an entry to ._entries."""
        table_name = entry.__table__.name
        if table_name not in self._entries:
            self._entries[table_name] = []
        self._entries[table_name].append(entry)

    def link(self, entry: sqm.SQLModel, field: str = None) -> "Staged":
        """Link entry of table to the staged data object.

        The table needs to be linked to dobject through a link table or by
        having a column referencing dobject.

        This method can be chained to link multiple entries of different tables.
        """
        dobject_id = self.dobject.id
        table_name = entry.__table__.name
        link_table = get_link_table(table_name, "dobject")
        # is there a link table that links the data object to the entry?
        if link_table is not None:
            model = table_meta.get_model(table_name)
            result = select(model).where(model.id == entry.id).one_or_none()
            if result is None:
                self._add_entry(entry)
            model = table_meta.get_model(link_table)
            link_entry = (
                select(model)
                .where(
                    model.dobject_id == dobject_id,
                    getattr(model, f"{table_name}_id") == entry.id,
                )
                .one_or_none()
            )
            if link_entry is None:
                # TODO: do not hard code column names
                link_entry = table_meta.get_model(link_table)(  # type: ignore
                    **{"dobject_id": dobject_id, f"{table_name}_id": entry.id}
                )
                self._add_entry(link_entry)
        # if there is no link table, does the entity have a column linking
        # to dobject?
        else:
            fks = get_foreign_keys(table_name, referred=("dobject", "id"))
            if ("dobject", "id") not in fks.values():
                raise RuntimeError(
                    "You can only link tables that have a foreign key or link table to"
                    " dobject."
                )
            else:
                if field is None:
                    if len(fks) > 1:
                        raise RuntimeError(
                            f"Cannot infer field name in {table_name} to populate with"
                            " dobject_id."
                        )
                    else:
                        field = list(fks.keys())[0]
                else:
                    if field not in fks:
                        raise RuntimeError(
                            f"Field name {field} does not exist in table {table_name}."
                        )
            setattr(entry, field, self.dobject.id)
            self._add_entry(entry)
        return self

    def link_features(
        self, knowledge_table, *, featureset_name: str = None
    ) -> "Staged":
        """Link dobject to features.

        Can be chained.

        Args:
            knowledge_table: a feature model instance
            featureset_name: name of the featureset

        Returns:
            writes to `Staged.knowledge_table`
        """
        # curate features
        # looks for the id column, if none is found, will assume in the index
        if self._dmem is None:
            self._dmem = load_to_memory(self._data)
        try:
            df = getattr(self._dmem, "var")  # for AnnData objects
            if callable(df):
                df = self._dmem
        except AttributeError:
            df = self._dmem
        # add feature entries
        # TODO: needs to be staged without adding here
        self._knowledge_table = LinkFeatureToKnowledgeTable(  # type: ignore
            df=df, knowledge_table=knowledge_table, featureset_name=featureset_name
        )
        return self

    @property
    def linked(self) -> dict:
        """Linked db entries of the dobject."""
        return self._entries

    @property
    def _datalog(self) -> dict:
        """Logging of a dobject entry."""
        return dict(dobject=f"{self._filepath.name} ({self.dobject.id})")

    def unlink(self) -> None:
        """Unstage all linked entries."""
        self._entries = {}

    def _commit_dobject(self, use_fsspec: bool = False) -> None:
        """Store and add dobject and its linked entries."""
        dobject_storage_key = f"{self.dobject.id}{self.dobject.suffix}"

        if self.dobject.suffix != ".zarr":
            size = store_file(
                self._filepath, dobject_storage_key, use_fsspec=use_fsspec
            )
        else:
            # adata size
            size = size_adata(self._dmem)
            storepath = settings.instance.storage.key_to_filepath(dobject_storage_key)
            print_progress = partial(print_hook, filepath=self._filepath)
            write_adata_zarr(self._dmem, storepath, callback=print_progress)
        self.dobject.size = size

        # ensure storage is populated
        storage = select(core.storage, root=str(settings.instance.storage_root)).one()
        self.dobject.storage_id = storage.id

        # populate dtransform_id
        self.dobject.dtransform_id = self._dtransform.id

        # add dobject first to satisfy foreign key constraints
        add(self.dobject)

        # add features and link to dobject
        if self._knowledge_table:
            self._knowledge_table.commit(self.dobject)

        track_usage(self.dobject.id, usage_type="ingest")

    def _commit_entries(self) -> None:
        for rows in self.linked.values():
            add(rows)


def compute_checksum(path: Path):
    # based on https://stackoverflow.com/questions/3431825/generating-an-md5-checksum-of-a-file  # noqa
    hash_md5 = hashlib.md5()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(4096), b""):
            hash_md5.update(chunk)
    hash_b64 = base64.urlsafe_b64encode(hash_md5.digest()).decode("ascii").strip("=")
    # the following can be commented out over time
    assert (
        base64.urlsafe_b64decode(f"{hash_b64}==".encode()).hex() == hash_md5.hexdigest()
    )  # noqa
    return hash_b64
