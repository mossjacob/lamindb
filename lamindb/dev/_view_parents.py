from typing import List, Union

from lnschema_core import ORM, File, Run


def view_lineage(file: File):
    """Graph of data lineage."""
    import graphviz

    all_runs = _get_all_parent_runs(file)
    df_edges = _df_edges_from_runs(all_runs)

    file_label = _label_file_run(file)

    u = graphviz.Digraph(
        file.id, node_attr={"fillcolor": "antiquewhite", "color": "orange"}
    )
    u.node(file.id, label=file_label, style="filled", fillcolor="orange", shape="oval")
    for _, row in df_edges.iterrows():
        if isinstance(row["source_record"], Run):
            style = "rounded"
            if row["source_record"].transform.type == "notebook":
                shape = "note"
            else:
                shape = "cds"
        else:
            shape = "oval"
            style = "filled"
        u.node(row["source"], label=row["source_label"], shape=shape, style=style)
        u.edge(row["source"], row["target"], color="dimgrey")

    return u


def view_parents(record: ORM, field: str, distance: int = 100):
    """Graph of parents."""
    if not hasattr(record, "parents"):
        return NotImplementedError(
            f"Parents view is not supported for {record.__class__.__name__}!"
        )
    import graphviz

    df_edges = _df_edges_from_parents(record=record, field=field, distance=distance)

    record_label = record.__getattribute__(field)

    u = graphviz.Digraph(record.id, node_attr={"color": "mediumseagreen"})
    u.node(
        record_label.replace(":", "_"),
        label=record_label,
        style="filled",
        color="mediumseagreen",
    )
    for _, row in df_edges.iterrows():
        u.node(row["source"], label=row["source_label"])
        u.edge(row["source"], row["target"], color="darkslategrey")

    return u


def _get_parents(record: ORM, field: str, distance: int):
    """Recursively get parent records within a distance."""
    model = record.__class__
    condition = f"children__{field}"
    results = model.select(**{condition: record.__getattribute__(field)}).all()
    if distance < 2:
        return results

    d = 2
    while d < distance:
        condition = "children__" + condition
        records = model.select(**{condition: record.__getattribute__(field)}).all()

        if len(records) == 0:
            return results

        results = results | records
        d += 1
    return results


def _df_edges_from_parents(record: ORM, field: str, distance: int):
    """Construct a DataFrame of edges as the input of graphviz.Digraph."""
    parents = _get_parents(record=record, field=field, distance=distance)
    records = parents | record.__class__.objects.filter(id=record.id)
    df = records.distinct().df(include=[f"parents__{field}"])
    df_edges = df[[f"parents__{field}", field]]
    df_edges = df_edges.explode(f"parents__{field}")
    df_edges.dropna(axis=0, inplace=True)
    df_edges.rename(
        columns={f"parents__{field}": "source", field: "target"}, inplace=True
    )
    df_edges = df_edges.drop_duplicates()

    # colons messes with the node formatting:
    # https://graphviz.readthedocs.io/en/stable/node_ports.html
    df_edges["source_label"] = df_edges["source"]
    df_edges["target_label"] = df_edges["target"]
    df_edges["source"] = df_edges["source"].str.replace(":", "_")
    df_edges["target"] = df_edges["target"].str.replace(":", "_")
    return df_edges


def _get_all_parent_runs(file: File):
    """Get all input file runs recursively."""
    all_runs = {file.run}

    runs = [file.run]
    while any([r.inputs.exists() for r in runs if r is not None]):
        inputs = []
        for r in runs:
            inputs += r.inputs.all()
        runs = [f.run for f in inputs]
        all_runs.update(runs)
    return all_runs


def _label_file_run(record: Union[File, Run]):
    if isinstance(record, File):
        return f"{record.key}\nid:{record.id}" if record.key is not None else record.id
    elif isinstance(record, Run):
        return f"{record.transform.name}\nid:{record.id}"


def _df_edges_from_runs(all_runs: List[Run]):
    import pandas as pd

    df_values = []
    for run in all_runs:
        if run is None:
            continue
        if run.inputs.exists():
            df_values.append((list(run.inputs.all()), run))
        if run.outputs.exists():
            df_values.append((run, list(run.outputs.all())))
    df = pd.DataFrame(df_values, columns=["source_record", "target_record"])
    df = df.explode("source_record")
    df = df.explode("target_record")
    df["source"] = [i.id for i in df["source_record"]]
    df["target"] = [i.id for i in df["target_record"]]
    df["source_label"] = df["source_record"].apply(_label_file_run)
    return df