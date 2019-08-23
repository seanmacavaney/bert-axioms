"""This script implements an end-to-end feature extraction and training for MSMarco dataset."""

import argparse
import logging
import subprocess
import os
import sys
from msmarco_dataset import MsMarcoDataset
from trecrun_to_bert import TRECrun_to_BERT
from args_parser import getArgs


def get_path(home_dir, x):
    return os.path.join(home_dir, x)


def run_retrieval_step(data_dir, k, anserini_path, overwrite=False):
    """Runs a retrieval step using Anserini"""
    index_path = get_path(data_dir, "lucene-index.msmarco")
    assert os.path.isdir(
        index_path), "Index named {} not found!".format(index_path)

    # Run retrieval step for trainning dataset
    train_rank_path = "msmarco-train-top_{}.run".format(k)
    if not os.path.isfile(train_rank_path) or overwrite:
        topics_file = get_path(data_dir, "msmarco-doctrain-queries.tsv")
        assert os.path.isfile(
            topics_file), "could not find topics file {}".format(topics_file)
        output_path = get_path(
            data_dir, "msmarco_train_top-{}_bm25.run".format(k))

        cmd = """java -cp {} io.anserini.search.SearchCollection -topicreader Tsv\
             -index {} -topics {} -output {} -bm25 -k1=3.44 -b=0.87""".format(
            anserini_path, index_path, topics_file, output_path)
        subprocess.run(cmd)

    # count number of lines in generated file above
    with open(output_path) as f:
        for i, _ in enumerate(f):
            pass
    line_counter_train = i

    logging.info("Run file for train has %d lines", line_counter_train)
    # Generated BERT-Formatted triples
    TRECrun_to_BERT(output_path, data_dir, 'train', line_counter_train, k)


def main():
    print(sys.argv)
    args = getArgs(sys.argv[1:])

    logging.basicConfig(level=logging.getLevelName(args.log_level))

    if args.run_retrieval:
        run_retrieval_step(args.data_dir, args.k,
                           args.anserini_path, args.overwrite)

    # Dataset loader
    if args.train_file is not None:
        train_path = args.train_file
    else:
        train_path = get_path(args.data_dir, "tiny_sample.tsv")
    train_dataset = MsMarcoDataset(train_path, args.data_dir)

    # Fine tune


if __name__ == "__main__":
    main()
