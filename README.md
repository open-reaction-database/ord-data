# ord-data

![](https://github.com/Open-Reaction-Database/ord-data/workflows/Validation/badge.svg)
![](https://raw.githubusercontent.com/Open-Reaction-Database/ord-data/main/badges/reactions.svg)
[![DOI](https://zenodo.org/badge/283813042.svg)](https://zenodo.org/badge/latestdoi/283813042)

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

## Cloning with Git LFS

We use [Git LFS](https://git-lfs.github.com) to efficiently store
the Dataset records that make up the ORD. To view these files locally, you'll
need to install Git LFS before cloning the repository.

## Contributing

Please see the [Submission Workflow](https://docs.open-reaction-database.org/en/latest/submissions.html) documentation. Make sure to review the [license](https://github.com/open-reaction-database/ord-data/blob/main/LICENSE) and [terms of use](https://github.com/open-reaction-database/ord-data/blob/main/CONTRIBUTING.md#terms-of-use).
