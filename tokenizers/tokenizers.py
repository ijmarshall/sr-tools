import re
import pdb

from nltk.tokenize import word_tokenize, wordpunct_tokenize, sent_tokenize
from nltk.metrics.agreement import AnnotationTask
from itertools import chain, izip
from functools import wraps
from scipy.stats import describe
from collections import defaultdict, deque
from nltk.tokenize.punkt import *

from pprint import pprint

import parse_annotations


from indexnumbers import swap_num


import configparser # easy_install configparser



config = configparser.ConfigParser()
config.read('CNLP.INI')
base_path = config["Paths"]["base_path"]



def filters(func):
    """
    used as decorator
    allows pipeline functions to return helpful
    views/permetations of output data - flattened lists
    and filters based on the base (hidden) features
    """
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        
        flatten = kwargs.pop("flatten", False)
        filter = kwargs.pop("filter", None)

        raw_output = func(self, *args, **kwargs)
        if filter:
            filtered_output = [[raw_word for raw_word, base_word in izip(raw_sent, base_sent) if filter(base_word)]
                                for raw_sent, base_sent in izip(raw_output, self.functions)]
        else:
            filtered_output = raw_output

        if flatten:
            return [item for sublist in filtered_output for item in sublist]
        else:
            return filtered_output
    return wrapper




sent_tokenizer = PunktSentenceTokenizer()



class CochraneNLPLanguageVars(PunktLanguageVars):
    _re_non_word_chars   = r"(?:[?!)\";}\]\*:@\'\({\[=\.])" # added =
    """Characters that cannot appear within words"""

    _re_word_start    = r"[^\(\"\`{\[:;&\#\*@\)}\]\-,=]" # added =
    """Excludes some characters from starting word tokens"""


class newPunktWordTokenizer(TokenizerI):
    """
    taken from new version of NLTK 3.0 alpha
    to allow for span tokenization of words (current
    full version does not allow this)
    """
    def __init__(self, lang_vars=CochraneNLPLanguageVars()):
        self._lang_vars = lang_vars
        

    def tokenize(self, text):
        return self._lang_vars.word_tokenize(text)

    def span_tokenize(self, text):
        """
        Given a text, returns a list of the (start, end) spans of words
        in the text.
        """
        return [(sl.start, sl.stop) for sl in self._slices_from_text(text)]

    def _slices_from_text(self, text):
        last_break = 0
        contains_no_words = True
        for match in self._lang_vars._word_tokenizer_re().finditer(text):
            contains_no_words = False
            context = match.group()
            yield slice(match.start(), match.end())
        if contains_no_words:
            yield slice(0, 0) # matches PunktSentenceTokenizer's functionality

word_tokenizer = newPunktWordTokenizer()


def memo(func):
    cache = {}
    @wraps(func)
    def wrap(*args):
        if args not in cache:
            cache[args] = func(*args)
        return cache[args]
    return wrap

def flatten(nested_list):
    return [item for sublist in nested_list for item in sublist]

@memo
def get_abstracts(annotator):
    """
    Take the annotators abstract files
    """
    annotated_abstracts_file = base_path + "drug_trials_in_cochrane_" + annotator + ".txt"
    with open (annotated_abstracts_file, "r") as file:
        data=file.read()

    def clean(abstract):
        text = (re.split("BiviewID [0-9]*; PMID ?[0-9]*", abstract)[0]).strip()
        # text = re.sub('[nN]=([1-9]+[0-9]*)', r'N = \1', text) # not needed any more since change to tokenization
        return text
    return [clean(abstract) for abstract in re.split('Abstract \d+ of \d+', data)][1:]

def get_annotations(abstract_nr, annotator, convert_numbers=False):
    '''
    if convert_numbers is True, numerical strings (e.g., "twenty-five")
    will be converted to number ("25").
    '''
    abstract = get_abstracts(annotator)[abstract_nr]
    if convert_numbers:
        abstract = swap_num(abstract)


    tags = tag_words(abstract)


    # tags = p.get_tags(flatten=True) # returns a list of tags
    return tags


def split_tag_data(tagged_text):
    """
    takes in raw, tagged text
    gets tag indices, then removes all tags
    returns untagged_text, tag_positions
    (where tag_positions = position in untagged_text)
    """

    tag_pattern = '<(\/?[a-z0-9_]+)>'

    # tag_matches_a is indices in annotated text
    tag_matches = [(m.start(), m.end(), m.group(1)) for m in re.finditer(tag_pattern, tagged_text)]

    tag_positions = defaultdict(list)
    displacement = 0 # initial re.finditer gets indices in the tagged text
                     # this corrects and produces indices for untagged text

    for start, end, tag in tag_matches:
        tag_positions[start-displacement].append(tag)
        displacement += (end-start) # add on the current tag length to cumulative displacement

    untagged_text = re.sub(tag_pattern, "", tagged_text) # now remove all tags

    return untagged_text, tag_positions

def wordsent_span_tokenize(text):
    """
    first sentence tokenizes then word tokenizes *per sentence*
    adjusts word indices for the full text
    this guarantees no overlap of words over sentence boundaries
    """

    sent_indices = deque(sent_tokenizer.span_tokenize(text)) # use deques since lots of left popping later
    word_indices = deque() # use deques since lots of left popping later

    for s_start, s_end in sent_indices:
        word_indices.extend([(w_start + s_start, w_end + s_start) for w_start, w_end in word_tokenizer.span_tokenize(text[s_start:s_end])])

    return sent_indices, word_indices

@filters
def tag_words(tagged_text):
    """
    returns lists of (word, tag_list) tuples when given tagged text
    per *token* assumed (so mid word tags are extended to the whole word)
    """

    tagged_text = tagged_text.strip() # remove whitespace
    untagged_text, tag_indices = split_tag_data(tagged_text) # split the tagging data from the text
    
    

    # set up a few stacks at char, word, and sentence levels
    index_tag_stack = set() # tags active at current index

    char_stack = []
    current_word_tag_stack = set()
    # per word tagging, so if beginning of word token is tagged only, e.g. '<n>Fifty</n>-nine'
    # and 'Fifty-nine' was a single token, then we assume the whole

    word_stack = []

    sent_stack = []

    keep_char = False # whether we're keeping or discarding the current char
                      # (we'll keep at false unless within the indices of a word_token)

    sent_indices, word_indices = wordsent_span_tokenize(untagged_text)

    i = 0

    while i < len(untagged_text):

        # first process tag stack to see whether next words are tagged
        for tag in tag_indices[i]:
            if tag[0] == '/':
                try:
                    index_tag_stack.remove(tag[1:])
                except:
                    print tagged_text
                    print untagged_text[i-20:i+20]
                    raise ValueError('unexpected tag %s in position %d of text' % (tag, i))
            else:
                index_tag_stack.add(tag)


        if i == word_indices[0][1]: # if a word has ended
            keep_char = False
            word_stack.append((''.join(char_stack), list(current_word_tag_stack))) # push word and tag tuple to the word stack
            char_stack = [] # clear char stack
            current_word_tag_stack = set()
            word_indices.popleft() # remove current word

        if i == word_indices[0][0]:
            keep_char = True

        if keep_char:
            char_stack.append(untagged_text[i])
            current_word_tag_stack.update(index_tag_stack) # add any new tags
            # (keeps all tags no matter where they start inside a word,
            #  and the stack is cleared when move to a new work)

        if i == sent_indices[0][1]:
            sent_stack.append(word_stack)
            word_stack = []
            
            sent_indices.popleft()



        i += 1

    else:
        #if len(word_stack):
        if ''.join(char_stack) != "":
            word_stack.append((''.join(char_stack), list(current_word_tag_stack)))
            sent_stack.append(word_stack)

    
    #import pdb; pdb.set_trace()
    return sent_stack


def round_robin(abstract_nr, annotators = ["IJM", "BCW", "JKU"]):
    """
    Figure out who annotator A and B should be in a round-robin fashion
    """
    return [annotators[abstract_nr % len(annotators)],
            annotators[(abstract_nr + 1) % len(annotators)]]

def eliminate_order(tag):
    return re.sub('([0-9])((?:_a)?)', 'X\g<2>', tag)

def agreement_fn(a,b):
    # ignore the treatment number, tx1 is the same as tx2 (and tx1_a to tx2_a)
    def get_tag_set(string):
        return set([eliminate_order(x) for x in string.split('&')] if string else [])
    a = get_tag_set(a)
    b = get_tag_set(b)
    if len(a) == 0 and len(b) == 0:
        return 0.0
    else:
        # linearly scale (all agree = 0) (none agree = 1)
        return len(a.difference(b)) * (1 / float(max(len(a), len(b))))

def __str_combine_annotations(annotations_A, annotations_B):
    """
    Builds a string of annotations separate by an & for two annotators
    """
    a = [['A', idx, "&".join(x[1])] for idx, x in enumerate(annotations_A)]
    b = [['B', idx, "&".join(x[1])] for idx, x in enumerate(annotations_B)]
    return a + b

def calc_agreements(nr_of_abstracts=150):
    # Loop over the abstracts and calculate the kappa and alpha per abstract
    aggregate = []
    for i in range(0, nr_of_abstracts):
        # try:
            annotators = round_robin(i)
            annotations_A = flatten(get_annotations(i, annotators[0]))
            annotations_B = flatten(get_annotations(i, annotators[1]))
            annotations = __str_combine_annotations(annotations_A, annotations_B)
            a = AnnotationTask(annotations, agreement_fn)
            aggregate.append({
                "kappa" : a.kappa(),
                "alpha" : a.alpha(),
                "annotator_A" : annotators[0],
                "annotator_B" : annotators[1] })
        # except:
        #     print("Could not calculate kappa for abstract %i" % (i + 1))
        #     pass

    # Summary statistics
    kappa = describe([a['kappa'] for a in aggregate])
    print("number of abstracts %i" % kappa[0])
    print("[kappa] mean: " + str(kappa[2]))
    print("[kappa] variance: " + str(kappa[3]))
    alpha = describe([a['alpha'] for a in aggregate])
    print("[alpha] mean: " + str(alpha[2]))
    print("[alpha] variance: " + str(alpha[3]))

def merge_annotations(a, b, strategy = lambda a,b: a & b, preprocess = lambda x: x):
    """
    Returns the merging of a and b
    based on strategy (defaults to set intersection) Optionally takes
    a preprocess argument which takes a tag as argument and must
    return the processed tag

    example usage:
    print(merge_annotations(JKU1, BCW1, preprocess = eliminate_order))
    """
        
    result = []

    for sent_a, sent_b in izip(a, b):
        result_sent = []
        #print sent_a
        #print sent_b
        #print "\n"
        for (word_a, tag_list_a), (word_b, tag_list_b) in izip(sent_a, sent_b):
            if word_a != word_b:

                print "Mismatch:"
                print "Sentence A:"
                print sent_a
                print
                print "Sentence B:"
                print sent_b
                print "\n"
                print "word a: {0}; word b: {1}".format(word_a, word_b)
                raise Exception("Mismatch in abstract contents - please check tags! {0} vs {1}".format(len(a), len(b)))


            tag_set_a = set([preprocess(x) for x in tag_list_a])
            tag_set_b = set([preprocess(x) for x in tag_list_b])

            result_sent.append((word_a, list(strategy(tag_set_a, tag_set_b))))
        result.append(result_sent)
    return result


class MergedTaggedAbstractReader:

    def __init__(self, no_abstracts=200, merge_function=lambda a,b: a & b):
        self.load_abstracts(no_abstracts)
        # the function to merge two labels
        self.merge_function = merge_function

    def load_abstracts(self, no_abstracts, annotators=["IJM", "BCW", "JKU"]):

        def get_abstracts(annotator):
            return parse_annotations.get_abstracts(base_path + "drug_trials_in_cochrane_" + annotator + ".txt")

        # get three individual lists of abstracts (up to our maximum
        all_abstracts = [get_abstracts(annotator)[:no_abstracts] for annotator in annotators]

        # then zip into a list accessible by individual annotator ID
        all_abstracts = [{annotator: abstract for annotator, abstract in zip(annotators, abstracts)} for abstracts in zip(*all_abstracts)]
        self.abstracts = []

        for i, abstract in enumerate(all_abstracts):
            annotators = round_robin(i)

            # if not exclude hashtag in the combined string of both annotators notes
            if not re.search("\#(exclude|EXCLUDE)", ''.join(abstract[annotators[0]]["notes"] + abstract[annotators[1]]["notes"])):

                entry = {"biview_id": abstract[annotators[0]]["biviewid"],
                         "pmid": abstract[annotators[0]]["pmid"],
                         "file_id": abstract[annotators[0]]["annotid"],
                         "abstract a": abstract[annotators[0]]["abstract"],
                         "abstract b": abstract[annotators[1]]["abstract"]
                         }
              
                self.abstracts.append(entry)

    def __len__(self):
        return len(self.abstracts)

    def __getitem__(self, key):
        return self.abstracts[key]

    def get(self, key, **kwargs):
        return merge_annotations(self.get_annotations(
            self.abstracts[key]["abstract a"]), 
            self.get_annotations(self.abstracts[key]["abstract b"]), 
            strategy = self.merge_function,
            **kwargs)


    def get_file_id(self, file_id, **kwargs):
        for i in self:
            if i["file_id"] == file_id:
                return merge_annotations(self.get_annotations(i["abstract a"]), self.get_annotations(i["abstract b"]), **kwargs)
        else:
            raise IndexError("File ID %d not in reader" % (file_id,))

    def get_biview_id(self, biview_id, **kwargs):
        for i in self:
            if i["biview_id"] == biview_id:
                return merge_annotations(self.get_annotations(i["abstract a"]), self.get_annotations(i["abstract b"]), **kwargs)
        else:
            raise IndexError("BiViewer ID %d not in reader" % (biview_id,))

    def get_annotations(self, abstract, convert_numbers=True):
        '''
        if convert_numbers is True, numerical strings (e.g., "twenty-five")
        will be converted to number ("25").
        '''        
        if convert_numbers:
            abstract = swap_num(abstract)
            abstract = re.sub('(?:[0-9]+)\,(?:[0-9]+)', '', abstract)

        tags = tag_words(abstract)
        return tags

    def __iter__(self):
        self.abstract_index = 0
        return self

    def next(self):
        if self.abstract_index >= len(self):
            raise StopIteration
        else:
            self.abstract_index += 1
            return self.get(self.abstract_index-1)









        




# def remove_key(d, key):
#     if key in d:
#         r = dict(d)
#         del r[key]
#         return r
#     else:
#         return d

def merged_annotations(abstract_nr, **kwargs):
    """
    Determines the annotators for abstract_nr and returns
    the merged annotations for that abstract.
    All arguments are passed to merge_annotations

    example usage:
    merged_annotations(50, convert_numbers = True, preprocess = eliminate_order)
    """
    annotators = round_robin(abstract_nr)
    def ann(annotator):
        return get_annotations(abstract_nr, annotator, kwargs.pop("convert_numbers", False)) # pop = remove_key fn
    return merge_annotations(ann(annotators[1]), ann(annotators[0]), **kwargs)


def main():
    calc_agreements()
    # abstract = "Hello this is a sentence. Hello this is a sentence."
    # print tag_words(abstract)




if __name__ == "__main__":
    main()
    # m = MergedTaggedAbstractReader()

    # print len(m)

    # print get_abstracts("IJM")[0]
    # calc_agreements()
    # 

        
    

    # print merged_annotations(1)