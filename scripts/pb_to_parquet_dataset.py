# Copyright 2026 Open Reaction Database Project Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Converts one or more serialized Dataset files into a single Parquet file.

When multiple inputs are given, their reactions are concatenated into the output. Use
--name/--description/--dataset-id to set output metadata; otherwise the first input's
metadata is carried forward. A warning is logged if non-overridden metadata fields
disagree across inputs.

Inputs are streamed through the writer one file at a time, so peak memory is bounded by
the largest input (plus one row-group buffer) rather than the sum.
"""

import argparse
import dataclasses

from ord_schema import message_helpers, parquet
from ord_schema.logging import get_logger
from ord_schema.proto import dataset_pb2

logger = get_logger(__name__)


@dataclasses.dataclass(frozen=True)
class _Resolved:
    """Output metadata resolved from CLI overrides and/or the first input."""

    name: str
    description: str
    dataset_id: str | None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Convert serialized Dataset files to a single Parquet file."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Input Dataset files (.pb, .binpb, .pbtxt, .txtpb, .json, optionally "
        ".gz-compressed).",
    )
    parser.add_argument("--output", required=True, help="Output Parquet filename.")
    parser.add_argument("--name", default=None, help="Override output dataset name.")
    parser.add_argument(
        "--description", default=None, help="Override output dataset description."
    )
    parser.add_argument(
        "--dataset-id", default=None, help="Override output dataset_id."
    )
    parser.add_argument(
        "--row-group-size",
        type=int,
        default=1000,
        help="Rows per Parquet row group (default: 1000).",
    )
    return parser.parse_args(argv)


def main(args: argparse.Namespace) -> None:
    """Streams reactions from the input Dataset files into a single Parquet file."""
    if not args.inputs:
        raise ValueError("at least one input file is required")

    first = message_helpers.load_message(args.inputs[0], dataset_pb2.Dataset)
    resolved = _Resolved(
        name=args.name if args.name is not None else first.name,
        description=args.description
        if args.description is not None
        else first.description,
        dataset_id=args.dataset_id
        if args.dataset_id is not None
        else (first.dataset_id or None),
    )

    count = 0
    with parquet.DatasetWriter(
        args.output,
        name=resolved.name,
        description=resolved.description,
        dataset_id=resolved.dataset_id,
        row_group_size=args.row_group_size,
    ) as writer:
        count += _drain(first, writer, args.inputs[0], resolved, args)
        del first
        for filename in args.inputs[1:]:
            logger.info("Loading %s", filename)
            dataset = message_helpers.load_message(filename, dataset_pb2.Dataset)
            count += _drain(dataset, writer, filename, resolved, args)
            del dataset
    if count == 0:
        raise ValueError("no reactions were written; inputs were empty")
    logger.info("Wrote %d reactions to %s", count, args.output)


def _drain(
    dataset: dataset_pb2.Dataset,
    writer: parquet.DatasetWriter,
    filename: str,
    resolved: _Resolved,
    args: argparse.Namespace,
) -> int:
    if (
        args.dataset_id is None
        and dataset.dataset_id
        and dataset.dataset_id != resolved.dataset_id
    ):
        logger.warning(
            "%s: dataset_id %r differs from output %r",
            filename,
            dataset.dataset_id,
            resolved.dataset_id,
        )
    if args.name is None and dataset.name and dataset.name != resolved.name:
        logger.warning(
            "%s: name %r differs from output %r", filename, dataset.name, resolved.name
        )
    if (
        args.description is None
        and dataset.description
        and dataset.description != resolved.description
    ):
        logger.warning("%s: description differs from output", filename)
    writer.write_all(dataset.reactions)
    return len(dataset.reactions)


if __name__ == "__main__":
    main(parse_args())
