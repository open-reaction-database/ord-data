"""Creates reaction-related badges for ord-data."""

import glob
import os
import requests

from absl import app
from absl import flags
from absl import logging

from ord_schema.proto import dataset_pb2

FLAGS = flags.FLAGS
flags.DEFINE_string('root', None, 'ORD root.')
flags.DEFINE_string('output', None, 'Output SVG filename.')


def main(argv):
    del argv  # Only used by app.run().
    num_reactions = 0
    for filename in glob.glob(os.path.join(FLAGS.root, '*', '*.pb')):
        with open(filename, 'rb') as f:
            dataset = dataset_pb2.Dataset.FromString(f.read())
        logging.info('%s:\t%d', filename, len(dataset.reactions))
        num_reactions += len(dataset.reactions)
    args = {
        'label': 'Reactions',
        'message': num_reactions,
        'color': 'informational',
    }
    response = requests.get('https://img.shields.io/static/v1', params=args)
    with open(FLAGS.output, 'w') as f:
        f.write(response.text)


if __name__ == '__main__':
    flags.mark_flags_as_required(['root', 'output'])
    app.run(main)
