import os
import json
import argparse
import distance
from apted import APTED, Config
from apted.helpers import Tree
from lxml import etree, html
from collections import deque
from tqdm import tqdm
from eval.parallel import parallel_process


class TableTree(Tree):
    def __init__(self, tag, colspan=None, rowspan=None, content=None, *children):
        self.tag = tag
        self.colspan = colspan
        self.rowspan = rowspan
        self.content = content
        self.children = list(children)

    def bracket(self):
        """Show tree using brackets notation"""
        if self.tag == 'td':
            result = '"tag": %s, "colspan": %d, "rowspan": %d, "text": %s' % \
                     (self.tag, self.colspan, self.rowspan, self.content)
        else:
            result = '"tag": %s' % self.tag
        for child in self.children:
            result += child.bracket()
        return "{{{}}}".format(result)


class CustomConfig(Config):
    @staticmethod
    def maximum(*sequences):
        """Get maximum possible value
        """
        return max(map(len, sequences))

    def normalized_distance(self, *sequences):
        """Get distance from 0 to 1
        """
        return float(distance.levenshtein(*sequences)) / self.maximum(*sequences)

    def rename(self, node1, node2):
        """Compares attributes of trees"""
        if (node1.tag != node2.tag) or (node1.colspan != node2.colspan) or (node1.rowspan != node2.rowspan):
            return 1.
        if node1.tag == 'td':
            if node1.content or node2.content:
                return self.normalized_distance(node1.content, node2.content)
        return 0.


class TEDS(object):
    ''' Tree Edit Distance basead Similarity
    '''
    def __init__(self, structure_only=False, n_jobs=1, ignore_nodes=None):
        assert isinstance(n_jobs, int) and (n_jobs >= 1), 'n_jobs must be an integer greather than 1'
        self.structure_only = structure_only
        self.n_jobs = n_jobs
        self.ignore_nodes = ignore_nodes
        self.__tokens__ = []

    def tokenize(self, node):
        ''' Tokenizes table cells
        '''
        self.__tokens__.append('<%s>' % node.tag)
        if node.text is not None:
            self.__tokens__ += list(node.text)
        for n in node.getchildren():
            self.tokenize(n)
        if node.tag != 'unk':
            self.__tokens__.append('</%s>' % node.tag)
        if node.tag != 'td' and node.tail is not None:
            self.__tokens__ += list(node.tail)

    def load_html_tree(self, node, parent=None):
        ''' Converts HTML tree to the format required by apted
        '''
        global __tokens__
        if node.tag == 'td':
            if self.structure_only:
                cell = []
            else:
                self.__tokens__ = []
                self.tokenize(node)
                cell = self.__tokens__[1:-1].copy()
            new_node = TableTree(node.tag,
                                 int(node.attrib.get('colspan', '1')),
                                 int(node.attrib.get('rowspan', '1')),
                                 cell, *deque())
        else:
            new_node = TableTree(node.tag, None, None, None, *deque())
        if parent is not None:
            parent.children.append(new_node)
        if node.tag != 'td':
            for n in node.getchildren():
                self.load_html_tree(n, new_node)
        if parent is None:
            return new_node

    def evaluate(self, pred, true):
        ''' Computes TEDS score between the prediction and the ground truth of a
            given sample
        '''
        if (not pred) or (not true):
            return 0.0
        pred = "<html>" + pred + "</html>"
        true = "<html>" + true + "</html>"
        parser = html.HTMLParser(remove_comments=True, encoding='utf-8')
        pred = html.fromstring(pred, parser=parser)
        true = html.fromstring(true, parser=parser)
        if pred.xpath('body/table') and true.xpath('body/table'):
            pred = pred.xpath('body/table')[0]
            true = true.xpath('body/table')[0]
            if self.ignore_nodes:
                etree.strip_tags(pred, *self.ignore_nodes)
                etree.strip_tags(true, *self.ignore_nodes)
            n_nodes_pred = len(pred.xpath(".//*"))
            n_nodes_true = len(true.xpath(".//*"))
            n_nodes = max(n_nodes_pred, n_nodes_true)
            tree_pred = self.load_html_tree(pred)
            tree_true = self.load_html_tree(true)
            distance = APTED(tree_pred, tree_true, CustomConfig()).compute_edit_distance()
            return 1.0 - (float(distance) / n_nodes)
        else:
            return 0.0
        
    def batch_evaluate(self, pred_json, true_json):
        ''' Computes TEDS score between the prediction and the ground truth of
            a batch of samples
            @params pred_json: {'FILENAME': 'HTML CODE', ...}
            @params true_json: {'FILENAME': {'html': 'HTML CODE'}, ...}
            @output: {'FILENAME': 'TEDS SCORE', ...}
        '''
        samples = true_json.keys()
        if self.n_jobs == 1:
            scores = [self.evaluate(pred_json.get(filename, ''), true_json[filename]['html']) for filename in tqdm(samples)]
        else:
            inputs = [{'pred': pred_json.get(filename, ''), 'true': true_json[filename]['html']} for filename in samples]
            scores = parallel_process(inputs, self.evaluate, use_kwargs=True, n_jobs=self.n_jobs, front_num=1)
        total_score_simple = 0
        num_simple = 0
        total_score_complex = 0
        num_complex = 0
        total_score = 0
        num_total = 0
        for filename,score in zip(samples, scores):
            print(filename)
            print(score)
            print('')
            if true_json[filename]['type'] == 'simple':
                total_score_simple += score
                num_simple += 1
            elif true_json[filename]['type'] == 'complex':
                total_score_complex += score
                num_complex += 1
            else:
                raise ValueError('Unknown type: %s' % true_json[filename]['type'])
            total_score += score
            num_total += 1
        if num_simple > 0:
            avg_score_simple = total_score_simple / num_simple
        else:
            avg_score_simple = 0
        if num_complex > 0:
            avg_score_complex = total_score_complex / num_complex
        else:
            avg_score_complex = 0
        avg_score = total_score / num_total
        print({'simple': (num_simple,avg_score_simple), 'complex': (num_complex,avg_score_complex), 'total': (num_total,avg_score)})

def main():
    parser = argparse.ArgumentParser(description="Evaluate page_to_markdown task")
    parser.add_argument(
        "workspace",
        help="The filesystem path where work will be stored, can be a local folder",
    )
    parser.add_argument(
        "--gt_file",
        help="Ground truth file",
    )
    parser.add_argument("--n_jobs", type=int, default=40, help="Number of jobs to run in parallel")
    args = parser.parse_args()
    
    pred_data = {}
    root_dir = os.path.join(args.workspace, "results")
    for jsonl_file in os.listdir(root_dir):
        if jsonl_file.endswith(".jsonl"):
            with open(os.path.join(root_dir, jsonl_file), "r") as f:
                for line in f:
                    data = json.loads(line)
                    pred_data[os.path.basename(data['orig_path'])] = data['document_text']

    gt_data = {}
    with open(args.gt_file, "r") as f:
        for line in f:
            data = json.loads(line)
            gt_data[data['image_name']] = {'html':data['gt_table'], 'type':data['type']}

    teds = TEDS(n_jobs=args.n_jobs, ignore_nodes=['b', 'thead', 'tbody'])
    teds.batch_evaluate(pred_data, gt_data)

if __name__ == "__main__":
    main()