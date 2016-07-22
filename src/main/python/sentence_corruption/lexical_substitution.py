from __future__ import print_function
import sys
import pickle
import codecs
import argparse
import random
from nltk.tokenize import word_tokenize

from index_data import DataIndexer
from keras.models import Model, model_from_json
from keras.layers import Input, LSTM, Embedding, Dropout, merge, TimeDistributed, Dense, SimpleRNN
from keras.callbacks import EarlyStopping

class WordReplacer(object):
    def __init__(self):
        self.data_indexer = DataIndexer()

    def process_data(self, sentences, max_length=None, factor_base=2, tokenize=True):
        #TODO: Deal with OOV
        sentence_lengths, indexed_sentences = self.data_indexer.index_data(sentences,
                max_length, tokenize)
        # We want the inputs to be words 0..n-1 and targets to be words 1..n in all
        # sentences, so that at each time step t, p(w_{t+1} | w_{0}..w_{t}) will be
        # predicted.
        input_array = indexed_sentences[:,:-1] # Removing the last word from each sentence
        target_array = indexed_sentences[:,1:] # Removing the first word from each sent.
        factored_target_arrays = self.data_indexer.factor_target_indices(target_array,
                base=factor_base)
        return sentence_lengths, input_array, factored_target_arrays

    def train_model(self, sentences, word_dim=50, factor_base=2, num_epochs=20,
                    tokenize=True, use_lstm=False):
        _, input_array, factored_target_arrays = self.process_data(sentences,
                factor_base=factor_base, tokenize=tokenize)
        vocab_size = self.data_indexer.get_vocab_size()
        num_factors = len(factored_target_arrays)
        model_input = Input(shape=input_array.shape[1:], dtype='int32') # (batch_size, num_words)
        embedding = Embedding(input_dim=vocab_size, output_dim=word_dim, mask_zero=True)
        embedded_input = embedding(model_input) # (batch_size, num_words, word_dim)
        regularized_embedded_input = Dropout(0.5)(embedded_input)
        # Bidirectional RNNs = Two RNNs, with the second one processing the input
        # backwards, and the two outputs concatenated.
        # Return sequences returns output at every timestep, instead of just the last one.
        rnn_model = LSTM if use_lstm else SimpleRNN
        # Since we will project the output of the lstm down to factor_base in the next
        # step anyway, it is okay to project it down a bit now. So output_dim = word_dim/2
        # This minimizes the number of parameters significantly. All four Ws in LSTM
        # will now be half as big as they would be if output_dim = word_dim.
        forward_rnn = rnn_model(output_dim=word_dim/2, return_sequences=True,
                name='forward_rnn')
        backward_rnn = rnn_model(output_dim=word_dim/2, go_backwards=True,
                return_sequences=True, name='backward_rnn')
        forward_rnn_out = forward_rnn(regularized_embedded_input) # (batch_size, num_words, word_dim/2)
        backward_rnn_out = backward_rnn(regularized_embedded_input) # (batch_size, num_words, word_dim/2)
        bidirectional_rnn_out = merge([forward_rnn_out, backward_rnn_out],
                mode='concat') # (batch_size, num_words, word_dim)
        regularized_rnn_out=Dropout(0.2)(bidirectional_rnn_out)
        model_outputs = []
        # Make as many output layers as there are factored target arrays, and the same size
        for i in range(num_factors):
            # TimeDistributed(layer) makes layer accept an additional time distribution.
            # i.e. if layer takes a n-dimensional input, TimeDistributed(layer) takes a
            # n+1 dimensional input, where the second dimension is time (or words in the
            # sentence). We need this now because RNN above returns one output per timestep
            factor_output = TimeDistributed(Dense(output_dim=factor_base,
                    activation='softmax', name='factor_output_%d' % i))
            model_outputs.append(factor_output(regularized_rnn_out)) # (batch_size, num_words, factor_base)
        # We have num_factors number of outputs in the model. So, the effective output shape is
        # [(batch_size, num_words, factor_base)] * num_factors
        model = Model(input=model_input, output=model_outputs)
        model.compile(loss='categorical_crossentropy', optimizer='adam')
        print(model.summary(), file=sys.stderr)
        early_stopping = EarlyStopping()
        model.fit(input_array, factored_target_arrays, nb_epoch=num_epochs,
                validation_split=0.1, callbacks=[early_stopping])
        self.model = model

    def save_model(self, model_serialization_prefix):
        data_indexer_pickle_file = open("%s_di.pkl" % model_serialization_prefix, "wb")
        pickle.dump(self.data_indexer, data_indexer_pickle_file)
        model_config = self.model.to_json()
        model_config_file = open("%s_config.json" % model_serialization_prefix, "w")
        print(model_config, file=model_config_file)
        self.model.save_weights("%s_weights.h5" % model_serialization_prefix, overwrite=True)

    def get_model_input_shape(self):
        return self.model.get_input_shape_at(0)

    def load_model(self, model_serialization_prefix):
        self.data_indexer = pickle.load(open("%s_di.pkl" % model_serialization_prefix))
        self.model = model_from_json(open("%s_config.json" % model_serialization_prefix).read())
        self.model.load_weights("%s_weights.h5" % model_serialization_prefix)
        self.model.compile(loss='categorical_crossentropy', optimizer='adam')
        print(self.model.summary(), file=sys.stderr)

    def get_substitutes(self, sentences, locations, train_sequence_length,
            num_substitutes=5, tokenize=True, search_space_size=5000):
        '''
        sentences (list(str)): List of sentences with words that need to be substituted
        locations (list(int)): List of indices, the same size as sentences, containing
            indices of words in sentences that need to be substituted
        train_sequence_length (int): Length of sequences the model was trained on
        '''
        max_train_length = train_sequence_length+1 # +1 because the last word would be stripped
        sentence_lengths, indexed_sentences, _ = self.process_data(sentences,
                max_length=max_train_length, tokenize=tokenize)
        # All prediction factors shape: [(batch_size, num_words, factor_base)] * num_factors
        all_prediction_factors = self.model.predict(indexed_sentences)
        all_substitutes = []
        for sentence_id, (sentence_length, location) in enumerate(zip(sentence_lengths,
                locations)):
            # If sentence length is greater than the longest sentence seen during training,
            # data indexer will truncate it anyway. So, let's not make expect the predictions
            # to be longer than that.
            sentence_length = min(sentence_length, max_train_length)
            prediction_length = sentence_length - 1 # Ignore the starting <s> symbol
            # Each prediction factor is of the shape
            # (num_sentences, padding_length+num_words, factor_base)
            # We need to take the probability of word given by "location" in sentence
            # given by sentence_id. For that, we need to collect the probabilities over
            # all factors, remove padding, and then look up the factor probabilities at
            # index=location
            word_predictions = [predictions[sentence_id][-prediction_length:][location] for
                    predictions in all_prediction_factors]
            sorted_substitutes = self.data_indexer.unfactor_probabilities(word_predictions,
                    search_space_size)
            all_substitutes.append(sorted_substitutes[:num_substitutes])
        return all_substitutes

if __name__=="__main__":
    argparser = argparse.ArgumentParser(description="Generate lexical substitutes using a \
            bidirectional RNN")
    argparser.add_argument("--train_file", type=str, help="File with sentences to train on,\
            one per line.")
    argparser.add_argument("--test_file", type=str, help="File with sentences to replace words,\
            one per line.")
    argparser.add_argument("--word_dim", type=int, help="Word dimensionality, default=50",
            default=50)
    argparser.add_argument("--max_instances", type=int,
                           help="Maximum number of training examples to use")
    argparser.add_argument("--use_lstm", help="Use LSTM instead of simple RNN", action='store_true')
    argparser.add_argument("--factor_base", type=int, help="Base of factored indices, \
            default=2", default=2)
    argparser.add_argument("--search_space_size", type=int, help="Number of most frequent words \
            to search over as replacement candidates", default=5000)
    argparser.add_argument("--num_epochs", type=int, help="Maximum number of epochs (will\
            stop early), default=20", default=20)
    argparser.add_argument("--no_tokenize", help="Do not tokenize input", action='store_true')
    argparser.add_argument("--model_serialization_prefix", default="lexsub",
                           help="Prefix for saving and loading model files")
    argparser.add_argument("--output_file", help="Place to save corrupted test file")
    args = argparser.parse_args()
    tokenize = False if args.no_tokenize else True
    word_replacer = WordReplacer()
    if args.train_file is not None:
        print("Reading training data", file=sys.stderr)
        train_sentences = [x.strip() for x in codecs.open(args.train_file, "r", "utf-8")]
        random.shuffle(train_sentences)
        if args.max_instances is not None:
            train_sentences = train_sentences[:args.max_instances]
        word_replacer.train_model(train_sentences, factor_base=args.factor_base,
                                  word_dim=args.word_dim, num_epochs=args.num_epochs,
                                  tokenize=tokenize, use_lstm=args.use_lstm)
        word_replacer.save_model(args.model_serialization_prefix)
    else:
        print("Loading saved model", file=sys.stderr)
        word_replacer.load_model(args.model_serialization_prefix)

    if args.test_file is not None:
        if args.output_file is None:
            print("Need to specify where to save output with --output_file", file=sys.stderr)
            sys.exit(-1)
        print("Reading test data", file=sys.stderr)
        test_sentence_words = [word_tokenize(x.strip()) for x in codecs.open(args.test_file,
            "r", "utf-8")]
        test_sentences = []
        locations = []
        # Stop words. Do not replace these or let them be replacements.
        words_to_ignore = set(["<s>", "</s>", "PADDING", ".", ",", "of", "in", "by", "the",
                 "to", "and", "is", "a"])
        for words in test_sentence_words:
            if len(set(words).difference(words_to_ignore)) == 0:
                # This means that there are no non-stop words in the input. Ignore it.
                continue
            # Generate a random location, between 0 and the second last position
            # because the last position is usually a period
            location = random.randint(0, len(words) - 2)
            while words[location] in words_to_ignore:
                location = random.randint(0, len(words) - 2)
            locations.append(location)
            test_sentences.append(" ".join(words))
        train_sequence_length = word_replacer.get_model_input_shape()[1]
        print >>sys.stderr, "Limiting search space size to %d" % args.search_space_size
        substitutes = word_replacer.get_substitutes(test_sentences, locations,
                train_sequence_length, tokenize=tokenize, 
                search_space_size=args.search_space_size)
        outfile = codecs.open(args.output_file, "w", "utf-8")
        for logprob_substitute_list, words, location in zip(substitutes, test_sentence_words,
                locations):
            word_being_replaced = words[location]
            for _, substitute in logprob_substitute_list:
                if substitute not in set(list(words_to_ignore) + [word_being_replaced]):
                    corrupted_words = list(words)
                    corrupted_words[location] = substitute
                    print("%s\t%s" % (" ".join(words), " ".join(corrupted_words)), file=outfile)
                    break
        outfile.close()