from typing import Dict, Iterable, Optional, Tuple, Union

import anndata as ad
import pandas as pd
from lamin_utils import logger
from lamindb_setup.dev._docs import doc_args
from lnschema_core import Modality
from lnschema_core.models import Dataset, Feature, FeatureSet
from lnschema_core.types import AnnDataLike, DataLike, FieldAttr

from lamindb._utils import attach_func_to_class_method
from lamindb.dev._data import _track_run_input
from lamindb.dev.storage._backed_access import AnnDataAccessor, BackedAccessor
from lamindb.dev.versioning import get_ids_from_old_version, init_id

from . import _TESTING, File, Run
from ._file import parse_feature_sets_from_anndata
from ._registry import init_self_from_db
from .dev._data import (
    add_transform_to_kwargs,
    get_run,
    save_feature_set_links,
    save_transform_run_feature_sets,
)
from .dev.hashing import hash_set


def __init__(
    dataset: Dataset,
    *args,
    **kwargs,
):
    if len(args) == len(dataset._meta.concrete_fields):
        super(Dataset, dataset).__init__(*args, **kwargs)
        return None
    # now we proceed with the user-facing constructor
    if len(args) > 1:
        raise ValueError("Only one non-keyword arg allowed: data")
    data: Union[pd.DataFrame, ad.AnnData, File, Iterable[File]] = (
        kwargs.pop("data") if len(args) == 0 else args[0]
    )
    name: Optional[str] = kwargs.pop("name") if "name" in kwargs else None
    description: Optional[str] = (
        kwargs.pop("description") if "description" in kwargs else None
    )
    reference: Optional[str] = (
        kwargs.pop("reference") if "reference" in kwargs else None
    )
    reference_type: Optional[str] = (
        kwargs.pop("reference_type") if "reference_type" in kwargs else None
    )
    run: Optional[Run] = kwargs.pop("run") if "run" in kwargs else None
    is_new_version_of: Optional[Dataset] = (
        kwargs.pop("is_new_version_of") if "is_new_version_of" in kwargs else None
    )
    initial_version_id: Optional[str] = (
        kwargs.pop("initial_version_id") if "initial_version_id" in kwargs else None
    )
    version: Optional[str] = kwargs.pop("version") if "version" in kwargs else None
    feature_sets: Dict[str, FeatureSet] = (
        kwargs.pop("feature_sets") if "feature_sets" in kwargs else {}
    )
    if not len(kwargs) == 0:
        raise ValueError(
            f"Only data, name, run, description, reference, reference_type can be passed, you passed: {kwargs}"  # noqa
        )

    if is_new_version_of is None:
        provisional_id = init_id(version=version, n_full_id=20)
    else:
        if not isinstance(is_new_version_of, Dataset):
            raise TypeError("is_new_version_of has to be of type ln.Dataset")
        provisional_id, initial_version_id, version = get_ids_from_old_version(
            is_new_version_of, version, n_full_id=20
        )
        if name is None:
            name = is_new_version_of.name
    if version is not None:
        if initial_version_id is None:
            logger.info(
                "initializing versioning for this dataset! create future versions of it"
                " using ln.Dataset(..., is_new_version_of=old_dataset)"
            )

    run = get_run(run)
    # there are exactly two ways of creating a Dataset object right now
    # using exactly one file or using more than one file
    # init file
    if isinstance(data, (pd.DataFrame, ad.AnnData, File)):
        files = None
        if isinstance(data, File):
            file = data
            if file._state.adding:
                raise ValueError("Save file before creating dataset!")
            if not feature_sets:
                feature_sets = file.features._feature_set_by_slot
            else:
                if len(file.features._feature_set_by_slot) > 0:
                    logger.info("overwriting feature sets linked to file")
        else:
            log_hint = True if feature_sets is None else False
            file_is_new_version_of = (
                is_new_version_of.file if is_new_version_of is not None else None
            )
            file = File(
                data,
                run=run,
                description="tmp",
                log_hint=log_hint,
                version=version,
                is_new_version_of=file_is_new_version_of,
            )
        hash = file.hash  # type: ignore
        provisional_id = file.id  # type: ignore
        file.description = f"See dataset {provisional_id}"  # type: ignore
        file._feature_sets = feature_sets
    # init files
    else:
        file = None
        if hasattr(data, "__getitem__"):
            assert isinstance(data[0], File)  # type: ignore
            files = data
            hash, feature_sets = from_files(files)  # type: ignore
        else:
            raise ValueError("Only DataFrame, AnnData and iterable of File is allowed")
    existing_dataset = Dataset.filter(hash=hash).one_or_none()
    if existing_dataset is not None:
        logger.warning(f"returning existing dataset with same hash: {existing_dataset}")
        init_self_from_db(dataset, existing_dataset)
        for slot, feature_set in dataset.features._feature_set_by_slot.items():
            if slot in feature_sets:
                if not feature_sets[slot] == feature_set:
                    dataset.feature_sets.remove(feature_set)
                    logger.warning(f"removing feature set: {feature_set}")
    else:
        kwargs = {}
        add_transform_to_kwargs(kwargs, run)
        super(Dataset, dataset).__init__(
            id=provisional_id,
            name=name,
            description=description,
            reference=reference,
            reference_type=reference_type,
            file=file,
            hash=hash,
            run=run,
            version=version,
            initial_version_id=initial_version_id,
            **kwargs,
        )
    dataset._files = files
    dataset._feature_sets = feature_sets


@classmethod  # type: ignore
@doc_args(Dataset.from_df.__doc__)
def from_df(
    cls,
    df: "pd.DataFrame",
    field: FieldAttr = Feature.name,
    name: Optional[str] = None,
    description: Optional[str] = None,
    run: Optional[Run] = None,
    modality: Optional[Modality] = None,
    reference: Optional[str] = None,
    reference_type: Optional[str] = None,
) -> "Dataset":
    """{}"""
    feature_set = FeatureSet.from_df(df, field=field, modality=modality)
    if feature_set is not None:
        feature_sets = {"columns": feature_set}
    else:
        feature_sets = {}
    dataset = Dataset(
        data=df, name=name, run=run, description=description, feature_sets=feature_sets
    )
    return dataset


@classmethod  # type: ignore
@doc_args(Dataset.from_anndata.__doc__)
def from_anndata(
    cls,
    adata: "AnnDataLike",
    field: Optional[FieldAttr],
    name: Optional[str] = None,
    description: Optional[str] = None,
    run: Optional[Run] = None,
    modality: Optional[Modality] = None,
    reference: Optional[str] = None,
    reference_type: Optional[str] = None,
) -> "Dataset":
    """{}"""
    if isinstance(adata, File):
        assert not adata._state.adding
        assert adata.accessor == "AnnData"
        adata_parse = adata.path
    else:
        adata_parse = adata
    feature_sets = parse_feature_sets_from_anndata(adata_parse, field, modality)
    dataset = Dataset(
        data=adata,
        run=run,
        name=name,
        description=description,
        feature_sets=feature_sets,
    )
    return dataset


# internal function, not exposed to user
def from_files(files: Iterable[File]) -> Tuple[str, Dict[str, str]]:
    # assert all files are already saved
    saved = not any([file._state.adding for file in files])
    if not saved:
        raise ValueError("Not all files are yet saved, please save them")
    # query all feature sets of files
    file_ids = [file.id for file in files]
    # query all feature sets at the same time rather than making a single query per file
    feature_set_file_links = File.feature_sets.through.objects.filter(
        file_id__in=file_ids
    )
    feature_set_slots_ids = {}
    for link in feature_set_file_links:
        feature_set_slots_ids[link.slot] = link.feature_set_id
    # validate consistency of hashes
    # we do not allow duplicate hashes
    hashes = [file.hash for file in files]
    if len(hashes) != len(set(hashes)):
        seen = set()
        non_unique = [x for x in hashes if x in seen or seen.add(x)]  # type: ignore
        raise ValueError(
            "Please pass files with distinct hashes: these ones are non-unique"
            f" {non_unique}"
        )
    hash = hash_set(set(hashes))
    return hash, feature_set_slots_ids


# docstring handled through attach_func_to_class_method
def backed(
    self, is_run_input: Optional[bool] = None
) -> Union["AnnDataAccessor", "BackedAccessor"]:
    _track_run_input(self, is_run_input)
    if self.file is None:
        raise RuntimeError("Can only call backed() for datasets with a single file")
    return self.file.backed()


# docstring handled through attach_func_to_class_method
def load(self, is_run_input: Optional[bool] = None, **kwargs) -> DataLike:
    _track_run_input(self, is_run_input)
    if self.file is not None:
        return self.file.load()
    else:
        all_files = self.files.all()
        suffixes = [file.suffix for file in all_files]
        if len(set(suffixes)) != 1:
            raise RuntimeError(
                "Can only load datasets where all files have the same suffix"
            )
        objects = [file.load() for file in all_files]
        file_ids = [file.id for file in all_files]
        if isinstance(objects[0], pd.DataFrame):
            return pd.concat(objects)
        elif isinstance(objects[0], ad.AnnData):
            return ad.concat(objects, label="file_id", keys=file_ids)


# docstring handled through attach_func_to_class_method
def delete(self, storage: Optional[bool] = None) -> None:
    super(Dataset, self).delete()
    if self.file is not None:
        self.file.delete(storage=storage)


# docstring handled through attach_func_to_class_method
def save(self, *args, **kwargs) -> None:
    if self.file is not None:
        self.file.save()
    # we don't need to save feature sets again
    save_transform_run_feature_sets(self)
    super(Dataset, self).save()
    if hasattr(self, "_files"):
        if self._files is not None and len(self._files) > 0:
            self.files.set(self._files)
    save_feature_set_links(self)


METHOD_NAMES = [
    "__init__",
    "from_anndata",
    "from_df",
    "backed",
    "load",
    "delete",
    "save",
]

if _TESTING:
    from inspect import signature

    SIGS = {
        name: signature(getattr(Dataset, name))
        for name in METHOD_NAMES
        if name != "__init__"
    }

for name in METHOD_NAMES:
    attach_func_to_class_method(name, Dataset, globals())
