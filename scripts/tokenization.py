from transformers import DistilBertTokenizer
import logging
from tqdm.auto import tqdm
import os
import pickle
import time
from tokenizers import BertWordPieceTokenizer
logging.getLogger("transformers").setLevel(logging.WARNING)


def tokenize_queries(config):
    # Tokenize queries from a tsv file
    tokenizer = DistilBertTokenizer.from_pretrained(config.bert_class)
    train_queries_path = os.path.join(config.data_home, "queries/msmarco-doctrain-queries.tsv")
    assert os.path.isfile(train_queries_path), "Train queries not found at {}".format(train_queries_path)

    bert_file_path = train_queries_path + ".bert"
    if ((not os.path.isfile(train_queries_path + ".tokenized"))
            or not os.path.isfile(bert_file_path)
            or "train_query_tokenizer" in config.force_steps):
        logging.info("tokenizing train queries")
        with open(train_queries_path) as inf, \
                open(train_queries_path + ".tokenized", 'w') as outf, \
                open(bert_file_path, 'w') as bertf:
            for line in tqdm(inf, total=config.train_queries, desc="tokenizing train queries"):
                q_id, query = line.split("\t")
                bert_query = tokenizer.tokenize(query)
                bertf.write("{}\t{}\n".format(q_id, bert_query))
                tokenized_query = ' '.join([x for x in bert_query]).replace("##", "")
                outf.write("{}\t{}\n".format(q_id, tokenized_query))
    else:
        logging.info("Already found tokenized train queries at %s", train_queries_path)

    # Tokenize dev queries
    dev_queries_path = os.path.join(config.data_home, "queries/msmarco-docdev-queries.tsv")
    assert os.path.isfile(dev_queries_path), "Dev queries not found at {}".format(dev_queries_path)
    bert_file_path = dev_queries_path + ".bert"
    if ((not os.path.isfile(dev_queries_path + ".tokenized"))
            or not os.path.isfile(bert_file_path)
            or "dev_query_tokenizer" in config.force_steps):
        logging.info("tokenizing dev queries")
        with open(dev_queries_path) as inf, \
                open(dev_queries_path + ".tokenized", 'w') as outf, \
                open(bert_file_path, 'w') as bertf:
            for line in tqdm(inf, total=config.full_dev_queries, desc="Tokenizing dev queries"):
                q_id, query = line.split("\t")
                bert_query = tokenizer.tokenize(query)
                bertf.write("{}\t{}\n".format(q_id, bert_query))
                tokenized_query = ' '.join([x for x in bert_query]).replace("##", "")
                outf.write("{}\t{}\n".format(q_id, tokenized_query))
    else:
        logging.info("Already found tokenized dev queries at %s", dev_queries_path)


def process_chunk(chunk_no, block_offset, no_lines, config):
    # Load lines
    doc_ids = []
    full_texts = []
    docs_path = os.path.join(config["data_home"], "docs/msmarco-docs.tsv")
    with open(docs_path, encoding="utf-8") as f:
        f.seek(block_offset[chunk_no])
        for i in tqdm(range(no_lines), desc="Loading block for {}".format(chunk_no)):
            line = f.readline()
            try:
                doc_id, url, title, text = line[:-1].split("\t")
            except (IndexError, ValueError):
                continue
            doc_ids.append(doc_id)
            full_texts.append(" ".join([url, title, text]))
    # tokenizer = DistilBertTokenizer.from_pretrained(config["bert_class"])
    tokenizer = BertWordPieceTokenizer(config["tokenizer_vocab_path"], lowercase=True)
    output_line_format = "{}\t{}\n"
    trec_format = "<DOC>\n<DOCNO>{}</DOCNO>\n<TEXT>{}</TEXT></DOC>\n"
    partial_doc_path = os.path.join(config["data_home"], "tmp", "docs-{}".format(chunk_no))
    partial_doc_path_bert = os.path.join(config["data_home"], "tmp", "docs-{}.bert".format(chunk_no))
    partial_trec_path = os.path.join(config["data_home"], "tmp", "trec_docs-{}".format(chunk_no))

    with open(partial_doc_path, 'w', encoding="utf-8") as outf, open(partial_trec_path, 'w', encoding="utf-8") as outf_trec, open(partial_doc_path_bert, 'w', encoding='utf-8') as outf_bert:  # noqa E501
        start = time.time()
        tokenized = tokenizer.encode_batch(full_texts)
        end = time.time()
        print("tokenizer {} finished in {}s".format(chunk_no, end - start), )
        for doc_id, sample in tqdm(zip(doc_ids, tokenized), desc="dumping tokenized docs to tmp file", total=len(tokenized)):  # noqa E501
            start = time.time()
            bert_text = sample.tokens[1:-1]
            tokenized_text = ' '.join(bert_text).replace("##", "")
            outf.write(output_line_format.format(doc_id, tokenized_text))
            outf_trec.write(trec_format.format(doc_id, tokenized_text))
            outf_bert.write("{}\t{}\n".format(doc_id, bert_text))
        outf.flush()
        outf_trec.flush()
        outf_bert.flush()


def tokenize_docs(config):
    """Tokenize docs, both tsv and TREC formats. Also generates offset file. Can take a LONG time"""
    if (os.path.isfile(os.path.join(config.data_home, "docs/msmarco-docs.tokenized.tsv"))
                                and "doc_tokenizer" not in config.force_steps):  # noqa
        logging.info("tokenized docs tsv files already found at %s.",
                     os.path.join(config.data_home, "docs/msmarco-docs.tokenized.*"))
        return

    docs_path = os.path.join(config.data_home, "docs/msmarco-docs.tsv")
    assert os.path.isfile(docs_path), "Could not find documents file at {}".format(docs_path)
    # Load in memory, split blocks and run
    excess_lines = config.corpus_size % config.number_of_cpus
    number_of_chunks = config.number_of_cpus
    if excess_lines > 0:
        number_of_chunks = config.number_of_cpus - 1
    block_offset = {}
    lines_per_chunk = config.corpus_size // number_of_chunks
    logging.info("Number of lines per CPU chunk: %i", lines_per_chunk)
    if not os.path.isdir(os.path.join(config.data_home, "tmp")):
        os.mkdir(os.path.join(config.data_home, "tmp"))
    if config.number_of_cpus < 2:
        block_offset[0] = 0
    elif not os.path.isfile(os.path.join(config.data_home, "block_offset_{}.pkl".format(config.number_of_cpus))):
        pbar = tqdm(total=config.corpus_size)
        with open(docs_path) as inf:
            current_chunk = 0
            counter = 0
            line = True
            while line:
                pbar.update()
                if counter % lines_per_chunk == 0:
                    block_offset[current_chunk] = inf.tell()
                    current_chunk += 1
                line = inf.readline()
                counter += 1
        pbar.close()
        pickle.dump(block_offset, open(os.path.join(config.data_home, "block_offset_{}.pkl".format(config.number_of_cpus)), 'wb'))  # noqa E501
    else:
        block_offset = pickle.load(open(os.path.join(config.data_home, "block_offset_{}.pkl".format(config.number_of_cpus)), 'rb'))  # noqa E501
    pbar = tqdm(total=config.number_of_cpus)
    assert len(block_offset) == config.number_of_cpus

    def update(*a):
        pbar.update()
    if config.number_of_cpus == 1:
        process_chunk(0, block_offset, lines_per_chunk, dict(config))
        return

    for i in range(len(block_offset)):
        process_chunk(i, block_offset, lines_per_chunk, dict(config))

    with open(os.path.join(config.data_home, "docs/msmarco-docs.tokenized.tsv"), 'w') as outf:
        for i in tqdm(range(config.number_of_cpus), desc="Merging tsv file"):
            partial_path = os.path.join(config.data_home, "tmp", "docs-{}".format(i))
            for line in open(partial_path):
                outf.write(line)
            os.remove(partial_path)

    with open(os.path.join(config.data_home, "docs/msmarco-docs.tokenized.trec"), 'w') as outf:
        for i in tqdm(range(config.number_of_cpus), desc="Merging TREC file"):
            partial_trec_path = os.path.join(config.data_home, "tmp", "trec_docs-{}".format(i))
            for line in open(partial_trec_path):
                outf.write(line)
            os.remove(partial_trec_path)

    with open(os.path.join(config.data_home, "docs/msmarco-docs.tokenized.bert"), 'w') as outf:
        for i in tqdm(range(config.number_of_cpus), desc="Merging BERT file"):
            partial_bert_path = os.path.join(config.data_home, "tmp", "docs-{}.bert".format(i))
            for line in open(partial_bert_path):
                outf.write(line)
            os.remove(partial_bert_path)
