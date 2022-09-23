from pathlib import Path
from shutil import SameFileError
from typing import Dict

from lnbfx import BfxRun
from lnbfx.dev import get_bfx_files_from_dir
from lndb_setup import settings
from lnschema_core import id

from lamindb.dev._core import get_name_suffix_from_filepath

from .._logger import colors, logger
from ..dev import format_pipeline_logs, storage_key_from_triple, track_usage
from ..dev.file import load_to_memory, store_file
from ..dev.object import infer_suffix, write_to_file
from ._insert import insert
from ._link import link
from ._query import query


class Ingest:
    """Ingest dobject."""

    def __init__(self) -> None:
        self._added: Dict = {}  # staged dobjects and pipeline runs
        self._feature_models: Dict = {}  # feature_models
        self._pipeline_runs: Dict = {}  # pipeline runs

    @property
    def status(self) -> list:
        """Staged dobjects and pipeline runs for ingestion."""
        added_list = []
        for k, v in self._added.items():
            entry = dict(filepath=k.as_posix(), dobject_id=v[0], dobject_v=v[1])
            if k in self._feature_models.keys():
                entry["feature_model"] = self._feature_models[k]
            added_list.append(entry)

        return added_list

    def add(
        self,
        dobject,
        *,
        name: str = None,
        feature_model=None,
        featureset_name: str = None,
        pipeline_run: BfxRun = None,
        dobject_id=None,
        dobject_v="1",
    ):
        """Stage a data object (in memory or file) for ingestion.

        Args:
            dobject: A data object in memory or filepath.
            name: A name. Required if passing in memory object.
            feature_model: Features to link during ingestion.
            featureset_name: Name of the featureset to be linked.
            pipeline_run: The instance of pipeline run, e.g. BfxRun.
            dobject_id: The dobject id.
            dobject_v: The dobject version.
        """
        # stage dobject
        filepath = IngestObject.add(
            self,  # type: ignore
            dobject=dobject,
            name=name,
            feature_model=feature_model,
            featureset_name=featureset_name,
            dobject_id=dobject_id,
            dobject_v=dobject_v,
        )

        # stage pipeline entities (runs, pipelines) and link their dobjects
        if pipeline_run is not None:
            IngestPipeline.add(self, pipeline_run, filepath)  # type: ignore

    def commit(self, jupynb_v=None):
        """Complete ingestion.

        Args:
            jupynb_v: Notebook version to publish. Is automatically set if `None`.
        """
        from nbproject import dev, meta, publish

        if meta.live.title is None:
            raise RuntimeError(
                "Can only ingest from notebook with title. Please set a title!"
            )

        jupynb_id = meta.store.id
        jupynb_v = dev.set_version(jupynb_v)  # version to be set in publish()
        jupynb_name = meta.live.title

        # ingest pipeline entities (pipeline runs, pipelines, dobjects from pipelines)
        pipeline_logs = IngestPipeline.commit(
            self,
            jupynb_id,
            jupynb_v,
            jupynb_name,
        )

        # ingest jupynb entities (dobjects)
        jupynb_logs = IngestObject.commit(
            self,
            jupynb_id,
            jupynb_v,
            jupynb_name,
        )

        # pretty logging info
        IngestPipeline.log(pipeline_logs)
        IngestObject.log(jupynb_logs)

        publish(calling_statement="commit(")


class IngestPipeline:
    def add(ingest, pipeline_run, path):
        """Setup and stage pipeline entities and link their dobjects."""
        # set pipeline run parameters
        IngestPipeline.setup(pipeline_run, path)
        # stage pipeline entities
        if path.is_dir():
            # parse directory and stage individual dobjects
            del ingest._added[path]  # do not ingest the directory
            dobjects_to_add = get_bfx_files_from_dir(path)
            for dobject in dobjects_to_add:
                ingest.add(dobject, pipeline_run=pipeline_run)
        else:
            # stage link between dobject and its pipeline run
            ingest._pipeline_runs[path] = pipeline_run

    def setup(pipeline_run, path):
        """Set pipeline run parameters for ingestion."""
        pipeline_run.db_engine = settings.instance.db_engine()
        if path.is_dir():
            pipeline_run.run_dir = path

    def commit(ingest, jupynb_id, jupynb_v, jupynb_name):
        """Ingest pipeline entities and their dobjects."""
        logs = []
        for run in set(ingest._pipeline_runs.values()):
            # ingest pipeline run and its pipeline
            IngestPipeline.ingest_run(run)
            # ingest dobjects from the pipeline run
            dobject_paths = [
                path
                for path, pipeline_run in ingest._pipeline_runs.items()
                if pipeline_run is run
            ]
            for filepath in dobject_paths:
                dobject_id, dobject_v = ingest._added.get(filepath)
                dobject_id = IngestObject.ingest_dobject(
                    filepath=filepath,
                    jupynb_id=jupynb_id,
                    jupynb_v=jupynb_v,
                    jupynb_name=jupynb_name,
                    dobject_id=dobject_id,
                    dobject_v=dobject_v,
                    pipeline_run=run,
                )
                # link dobject to its bionformatics metadata (bfxmeta)
                run.link_dobject(dobject_id, filepath)

                log_file_name = str(filepath.relative_to(run.run_dir))
                logs.append(
                    [
                        f"{log_file_name} ({dobject_id}, {dobject_v})",
                        f"{run.run_name} ({run.run_id})",
                        f"{settings.user.handle} ({settings.user.id})",
                    ]
                )

        return logs

    def log(logs):
        """Pretty print logs."""
        from tabulate import tabulate  # type: ignore

        if len(logs) == 0:
            return

        logs = format_pipeline_logs(logs)
        log_table = tabulate(
            logs,
            headers=[
                colors.green("dobject"),
                colors.blue("pipeline run"),
                colors.purple("user"),
            ],
            tablefmt="pretty",
            stralign="left",
        )

        logger.success(
            f"Ingested the following dobjects from pipeline runs:\n{log_table}"
        )

    def ingest_run(run):
        """Ingest pipeline run and its pipeline."""
        # check if core pipeline exists, insert if not
        df_pipeline = getattr(query, "pipeline")(as_df=True).all()
        if (run.pipeline_id, run.pipeline_v) not in df_pipeline.index:
            insert.pipeline(
                id=run.pipeline_id,
                v=run.pipeline_v,
                name=run.pipeline_name,
                reference=run.pipeline_reference,
            )
        # check if core pipeline run exists, insert if not
        df_pipeline_run = getattr(query, "pipeline_run")(as_df=True).all()
        if run.run_id not in df_pipeline_run.index:
            insert.pipeline_run(
                id=run.run_id,
                name=run.run_name,
                pipeline_id=run.pipeline_id,
                pipeline_v=run.pipeline_v,
            )
        # check if bfx pipeline and run exist, insert if not
        run.check_and_ingest()


class IngestObject:
    def add(
        ingest,
        dobject,
        name,
        feature_model,
        featureset_name,
        dobject_id,
        dobject_v,
    ):
        """Stage dobject for ingestion."""
        primary_key = (
            id.id_dobject() if dobject_id is None else dobject_id,
            dobject_v,
        )

        dmem = None
        if isinstance(dobject, (Path, str)):
            # if a file is given, create a dobject
            # TODO: handle compressed files
            filepath = Path(dobject)
        else:
            # if in-memory object is given, return the cache path
            dmem = dobject
            suffix = infer_suffix(dobject)
            if name is None:
                raise RuntimeError("Provide name if ingesting in memory data.")
            filepath = Path(f"{name}{suffix}")
        # ingest features via feature models
        if feature_model is not None:
            # load file into memory
            if isinstance(dobject, (Path, str)):
                dmem = load_to_memory(dobject)
            else:
                dmem = dobject
            # curate features
            # looks for the id column, if none is found, will assume in the index
            try:
                df = getattr(dmem, "var")  # for AnnData objects
                if callable(df):
                    df = dmem
            except AttributeError:
                df = dmem
            curated = link.feature_model(
                df=df, feature_model=feature_model, featureset_name=featureset_name
            )
            ingest._feature_models[filepath] = curated

        ingest._added[filepath] = primary_key

        if not filepath.exists() and dmem is not None:
            write_to_file(dmem, filepath)  # type: ignore

        return filepath

    def commit(ingest, jupynb_id, jupynb_v, jupynb_name):
        """Ingest staged dobjects."""
        logs = []
        for filepath, (dobject_id, dobject_v) in ingest._added.items():
            # skip dobject if linked to a pipeline (already ingested)
            if ingest._pipeline_runs.get(filepath) is not None:
                continue
            dobject_id = IngestObject.ingest_dobject(
                filepath=filepath,
                jupynb_id=jupynb_id,
                jupynb_v=jupynb_v,
                jupynb_name=jupynb_name,
                dobject_id=dobject_id,
                dobject_v=dobject_v,
                pipeline_run=None,
            )

            logs.append(
                [
                    f"{filepath.name} ({dobject_id}, {dobject_v})",
                    f"{jupynb_name!r} ({jupynb_id}, {jupynb_v})",
                    f"{settings.user.handle} ({settings.user.id})",
                ]
            )

            # ingest with feature models
            curated = ingest._feature_models.get(filepath)
            if curated is not None:
                curated["model"].ingest(dobject_id, curated["df_curated"])

        return logs

    def log(logs):
        """Pretty print logs."""
        from tabulate import tabulate  # type: ignore

        if len(logs) == 0:
            return

        log_table = tabulate(
            logs,
            headers=[
                colors.green("dobject"),
                colors.blue("jupynb"),
                colors.purple("user"),
            ],
            tablefmt="pretty",
            stralign="left",
        )

        logger.success(f"Ingested the following dobjects:\n{log_table}")

    def ingest_dobject(
        filepath,
        jupynb_id,
        jupynb_v,
        jupynb_name,
        dobject_id,
        dobject_v,
        pipeline_run,
    ):
        """Insert and store dobject."""
        name, suffix = get_name_suffix_from_filepath(filepath)

        if pipeline_run is None:
            dobject_id = insert.dobject_from_jupynb(
                name=name,
                suffix=suffix,
                jupynb_id=jupynb_id,
                jupynb_v=jupynb_v,
                jupynb_name=jupynb_name,
                dobject_id=dobject_id,
                dobject_v=dobject_v,
            )
        else:
            dobject_id = insert.dobject_from_pipeline(
                name=name,
                suffix=suffix,
                dobject_id=dobject_id,
                dobject_v=dobject_v,
                pipeline_run=pipeline_run,
            )

        dobject_storage_key = storage_key_from_triple(dobject_id, dobject_v, suffix)
        try:
            store_file(filepath, dobject_storage_key)
        except SameFileError:
            pass

        track_usage(dobject_id, dobject_v, usage_type="ingest")

        return dobject_id


ingest = Ingest()
