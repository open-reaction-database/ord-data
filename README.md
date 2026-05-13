# ord-data

![](https://github.com/Open-Reaction-Database/ord-data/workflows/Validation/badge.svg)
![](https://raw.githubusercontent.com/Open-Reaction-Database/ord-data/main/badges/reactions.svg)
[![DOI](https://zenodo.org/badge/283813042.svg)](https://zenodo.org/badge/latestdoi/283813042)

## Getting the Data

**We recommend downloading the dataset from
[Hugging Face](https://huggingface.co/datasets/open-reaction-database/ord-data)
instead of cloning this repository with Git LFS.** GitHub LFS bandwidth is a
shared, limited resource, and heavy cloning traffic can exhaust our monthly
quota and block downloads for everyone. The Hugging Face mirror has no such
limit.

### Option 1 (recommended): Download from Hugging Face

```bash
pip install -r scripts/requirements.txt
python scripts/download_from_huggingface.py
```

The script mirrors the `data/` directory from the Hugging Face dataset into
your local checkout. Pass `--allow-pattern 'data/4d/*.pb.gz'` (repeatable) to
download only a subset, or `--output-dir <path>` to write somewhere other
than the repository root. If you don't need the Git history, you can also
clone this repo *without* LFS objects and then run the script:

```bash
GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/open-reaction-database/ord-data.git
cd ord-data
python scripts/download_from_huggingface.py
```

### Option 2: Clone with Git LFS

If you have access to Git LFS bandwidth and need the `.pb.gz` files in place
as part of a normal clone, install [Git LFS](https://git-lfs.github.com)
before cloning. Please prefer Option 1 when possible so we don't exhaust the
shared LFS quota.

## Data Manipulation

The `ord-data` repository contains the Open Reaction Database (ORD) in Google's Protobuf binary
format, which is stored in the [`data`](data) directory. Currently, all the data are stored in e.g.
*.pb.gz format (compressed Protobuf binary files) for the sake of efficiency. The user can convert
the data into human readable text format, *.pb.txt.

```python
# import requirements
from ord_schema.message_helpers import load_message, write_message
from ord_schema.proto import dataset_pb2

# load the binary ord file
dataset = load_message("input_fname.pb.gz", dataset_pb2.Dataset)
# save the ord file as human readable text
write_message(dataset, "output_fname.pbtxt")
```

We can also convert ORD data into JSON format.

```python
# import requirements
import json

from ord_schema.message_helpers import load_message, write_message
from ord_schema.proto import dataset_pb2
from google.protobuf.json_format import MessageToJson

input_fname = "sample_file.pb.gz"
dataset = load_message(
    input_fname,
    dataset_pb2.Dataset,
)

# take one reaction message from the dataset for example
rxn = dataset.reactions[0]
rxn_json = json.loads(
    MessageToJson(
        message=rxn,
        including_default_value_fields=False,
        preserving_proto_field_name=True,
        indent=2,
        sort_keys=False,
        use_integers_for_enums=False,
        descriptor_pool=None,
        float_precision=None,
        ensure_ascii=True,
    )
)

print(f"We have converted the {input_fname} to JSON format shown as below, \n{rxn_json}")
```

## Contributing

Please see the [Submission Workflow](https://docs.open-reaction-database.org/en/latest/submissions.html) documentation. Make sure to review the [license](https://github.com/open-reaction-database/ord-data/blob/main/LICENSE) and [terms of use](https://github.com/open-reaction-database/ord-data/blob/main/CONTRIBUTING.md#terms-of-use).
