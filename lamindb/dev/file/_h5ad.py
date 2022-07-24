import anndata
from anndata import AnnData
from lndb_cli._settings import local_filepath


def h5ad_to_anndata(filekey) -> AnnData:
    """h5ad → AnnData."""
    return anndata.read(local_filepath(filekey))
