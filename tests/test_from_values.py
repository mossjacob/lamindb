import pandas as pd
import pytest


@pytest.fixture(scope="module")
def df():
    return pd.DataFrame(
        (
            ["T cell", "CL:0000084"],
            ["hepatocyte", "CL:0000182"],
            ["my new cell type", ""],
        ),
        columns=["cell_type", "cell_type_id"],
    )


def test_from_values_name(df):
    from lnschema_bionty import CellType

    result = CellType.from_values(df.cell_type, CellType.name)
    ids = [i.ontology_id for i in result]
    assert len(result) == 3
    assert ids == ["CL:0000084", "CL:0000182", None]
    assert result[0].bionty_source.entity == "CellType"
    assert result[2].bionty_source is None


def test_from_values_ontology_id(df):
    from lnschema_bionty import CellType

    result = CellType.from_values(df.cell_type_id, CellType.ontology_id)
    names = [i.name for i in result]
    assert len(result) == 2
    assert names == ["T cell", "hepatocyte"]
    assert result[0].bionty_source.entity == "CellType"


def test_from_values_multiple_match():
    from lnschema_bionty import Gene

    records = Gene.from_values(["ABC1", "PDCD1"], Gene.symbol, species="human")
    assert len(records) == 3


def test_from_values_species():
    from lnschema_bionty import Gene, settings

    settings._species = None

    with pytest.raises(AssertionError):
        Gene.from_values(["ABC1"], Gene.symbol)

    settings.species = "human"
    records = Gene.from_values(["ABC1"], Gene.symbol)
    assert records[0].ensembl_gene_id == "ENSG00000068097"

    settings.species = "mouse"
    records = Gene.from_values(["ABC1"], Gene.symbol)
    assert records[0].ensembl_gene_id == "ENSMUSG00000015243"
