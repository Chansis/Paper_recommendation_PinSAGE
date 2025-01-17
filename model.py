import pickle
import argparse

import torch
import torch.nn as nn
import pandas as pd
import numpy as np
import torchtext

from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import layers
import sampler as sampler_module
import evaluation

class PinSAGEModel(nn.Module):
    def __init__(self, full_graph, ntype, textsets, hidden_dims, n_layers):
        super().__init__()

        self.proj = layers.LinearProjector(full_graph, ntype, textsets, hidden_dims)
        self.sage = layers.SAGENet(hidden_dims, n_layers)
        self.scorer = layers.ItemToItemScorer(full_graph, ntype)

    def forward(self, pos_graph, neg_graph, blocks):
        h_item = self.get_repr(blocks)
        pos_score = self.scorer(pos_graph, h_item)
        neg_score = self.scorer(neg_graph, h_item)
        return (neg_score - pos_score + 1).clamp(min=0)

    def get_repr(self, blocks):
        h_item = self.proj(blocks[0].srcdata)
        h_item_dst = self.proj(blocks[-1].dstdata)
        return h_item_dst + self.sage(blocks, h_item)
        
def load_model(data_dict, device, args):
    gnn = PinSAGEModel(data_dict['graph'], data_dict['author_ntype'], data_dict['textset'], args.hidden_dims, args.num_layers).to(device)
    opt = torch.optim.Adam(gnn.parameters(), lr=args.lr)
    #if args.retrain:
    #    checkpoint = torch.load(args.save_path + '.pt', map_location=device)
    #else:
    #    checkpoint = torch.load(args.save_path, map_location=device)
    if args.retrain:
        with open(args.save_path + '.pkl', 'rb') as f:
            checkpoint = pickle.load(f)
    else:
        with open(args.save_path, 'rb') as f:
            checkpoint = pickle.load(f)

    gnn.load_state_dict(checkpoint['model_state_dict'])
    opt.load_state_dict(checkpoint['optimizer_state_dict'])

    return gnn, opt, checkpoint['epoch']

def prepare_dataset(data_dict, args):
    g = data_dict['graph']
    item_texts = data_dict['item_texts']
    paper_ntype = data_dict['paper_ntype']
    author_ntype = data_dict['author_ntype']

    # Assign user and movie IDs and use them as features (to learn an individual trainable
    # embedding for each entity)
    g.nodes[paper_ntype].data['id'] = torch.arange(g.number_of_nodes(paper_ntype))
    g.nodes[author_ntype].data['id'] = torch.arange(g.number_of_nodes(author_ntype))
    data_dict['graph'] = g

    # 로깅을 추가하여 노드 수와 특성 수 확인
    num_paper_nodes = g.number_of_nodes(data_dict['paper_ntype'])
    num_author_nodes = g.number_of_nodes(data_dict['author_ntype'])
    print(f"Number of paper nodes: {num_paper_nodes}, Number of author nodes: {num_author_nodes}")

    if 'item_texts' in data_dict and data_dict['item_texts']:
        for key, texts in data_dict['item_texts'].items():
            print(f"Number of texts for {key}: {len(texts)}")

            
    # Prepare torchtext dataset and vocabulary
    if not len(item_texts):
        data_dict['textset'] = None
    else:
        fields = {}
        examples = []
        for key, texts in item_texts.items():
            fields[key] = torchtext.data.Field(include_lengths=True, lower=True, batch_first=True)
        for i in range(g.number_of_nodes(author_ntype)):
            example = torchtext.data.Example.fromlist(
                [item_texts[key][i] for key in item_texts.keys()],
                [(key, fields[key]) for key in item_texts.keys()])
            examples.append(example)
            
        textset = torchtext.data.Dataset(examples, fields)
        for key, field in fields.items():
            field.build_vocab(getattr(textset, key))
            #field.build_vocab(getattr(textset, key), vectors='fasttext.simple.300d')
        data_dict['textset'] = textset

    return data_dict

def prepare_dataloader(data_dict, args):
    g = data_dict['graph']
    paper_ntype = data_dict['paper_ntype']
    author_ntype = data_dict['author_ntype']
    textset = data_dict['textset']
    # Sampler
    batch_sampler = sampler_module.ItemToItemBatchSampler(
        g, paper_ntype, author_ntype, args.batch_size)
    neighbor_sampler = sampler_module.NeighborSampler(
        g, paper_ntype, author_ntype, args.random_walk_length,
        args.random_walk_restart_prob, args.num_random_walks, args.num_neighbors,
        args.num_layers)
    collator = sampler_module.PinSAGECollator(neighbor_sampler, g, author_ntype, textset)
    dataloader = DataLoader(
        batch_sampler,
        collate_fn=collator.collate_train,
        num_workers=args.num_workers)

    dataloader_test = DataLoader(
        torch.arange(g.number_of_nodes(author_ntype)),
        batch_size=args.batch_size,
        collate_fn=collator.collate_test,
        num_workers=args.num_workers)
    dataloader_it = iter(dataloader)

    return dataloader_it, dataloader_test, neighbor_sampler
    
def train(data_dict, args):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    if device.type == 'cpu':
        print('Current using CPUs')
    else:
        print ('Current cuda device ', torch.cuda.current_device()) # check
    # Dataset
    data_dict = prepare_dataset(data_dict, args)
    dataloader_it, dataloader_test, neighbor_sampler = prepare_dataloader(data_dict, args)
    
    # Model
    if args.retrain:
        print('Loading pretrained model...')
        gnn, opt, start_epoch = load_model(data_dict, device, args)
    else:
        gnn = PinSAGEModel(data_dict['graph'], data_dict['author_ntype'], data_dict['textset'], args.hidden_dims, args.num_layers)
        opt = torch.optim.Adam(gnn.parameters(), lr=args.lr)
        start_epoch = 0

    if args.eval_epochs:
        g = data_dict['graph']
        author_ntype = data_dict['author_ntype']
        paper_ntype = data_dict['paper_ntype']
        paper_to_author_etype = data_dict['paper_to_author_etype']
        timestamp = data_dict['timestamp']
        #print(g.ndata) - mapping 확인 차
        nid_pmid_dict = {v: k for v, k in enumerate(list(g.ndata['PMID'].values())[0].numpy())}
        nid_aid_dict = {nid.item(): aid.item() for aid, nid in  zip(g.ndata['Author_id']['author'], g.ndata['id']['author'])}


    gnn = gnn.to(device)
    for epoch in tqdm(range(start_epoch, args.num_epochs + start_epoch)):
        gnn.train()
        for batch in range(args.batches_per_epoch):
            pos_graph, neg_graph, blocks = next(dataloader_it)
            for i in range(len(blocks)):
                blocks[i] = blocks[i].to(device)
            pos_graph = pos_graph.to(device)
            neg_graph = neg_graph.to(device)

            loss = gnn(pos_graph, neg_graph, blocks).mean()
            opt.zero_grad()
            loss.backward()
            opt.step()

        # Evaluate
        if not epoch:
            continue
        
        if args.eval_epochs and not epoch % args.eval_epochs:
            h_item = evaluation.get_all_emb(gnn, g.ndata['id'][author_ntype], data_dict['textset'], author_ntype, neighbor_sampler, args.batch_size, device)
            item_batch = evaluation.author_by_paper_batch(g, paper_ntype, author_ntype, paper_to_author_etype, timestamp, args)
            recalls = []
            precisions = [] 
            hitrates = []
            papers = []

            for i, nodes in enumerate(item_batch):
                '''
                nodes : 유저당 실제 인터랙션 노드들 [train 노드, test 노드 (8: 2비율)]
                '''
                # 실제 paper ID 탐색
                category = nid_pmid_dict[i]
                paper_id = data_dict['paper_category'][category]  # 실제 paper id
                label = data_dict['testset'][paper_id]  # 테스트 라벨 // PMID
                papers.append(paper_id)

                # 실제 저자 ID 탐색
                item = evaluation.node_to_author(nodes, nid_aid_dict, data_dict['author_category'])  # 저자 ID
                label_idx = [i for i, x in enumerate(item) if x in label]  # 라벨 인덱스

                # 아이템 추천
                nodes = [x for i, x in enumerate(nodes) if i not in label_idx]  # 라벨 인덱스 미포함 입력 학습용 노드
                h_nodes = h_item[nodes]
                h_center = torch.mean(h_nodes, axis=0)  # 중앙 임베딩  
                dist = h_center @ h_item.t()  # 행렬곱
                topk = dist.topk(args.k)[1].cpu().numpy()  # dist 크기 순서로 k개 추출
                topk = evaluation.node_to_author(topk, nid_aid_dict, data_dict['author_category'])  # ID 변환

                tp = [x for x in label if x in topk]
                if not tp:
                    recall, precision, hitrate = 0, 0, 0
                else:
                    recall = len(tp) / len(label) 
                    precision = len(tp) / len(topk)
                    hitrate = 1  # 하나라도 있음

                recalls.append(recall)
                precisions.append(precision)
                hitrates.append(hitrate)

            result_df = pd.DataFrame({'recall': recalls, 'precision': precisions, 'hitrate': hitrates})
            result_df = result_df.mean().apply(lambda x: round(x, 3))
            recall, precision, hitrate = result_df['recall'], result_df['precision'], result_df['hitrate']
            print(f'\tEpoch:{epoch}\tRecall:{recall}\tHitrate:{hitrate}\tPrecision:{precision}')

        if args.save_epochs:
            if not epoch % args.save_epochs:
#                torch.save({
#                'epoch': epoch,
#                'model_state_dict': gnn.state_dict(),
#                'optimizer_state_dict': opt.state_dict(),
#                'loss': loss
#                        }, args.save_path + '_' + str(epoch) + 'epoch.pt')
                state = {
                            'epoch': epoch,
                            'model_state_dict': gnn.state_dict(),
                            'optimizer_state_dict': opt.state_dict(),
                            'loss': loss
                            }

                            #  .pkl 형식으로 파일 저장
                with open(args.save_path + '_' + str(epoch) + 'epoch.pkl', 'wb') as f:
                    pickle.dump(state, f)
        
    return gnn, epoch+1, opt, loss

if __name__ == '__main__':
    # Arguments
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--random-walk-length', type=int, default=2)
    parser.add_argument('--random-walk-restart-prob', type=float, default=0.5)
    parser.add_argument('--num-random-walks', type=int, default=10)
    parser.add_argument('--num-neighbors', type=int, default=3)
    parser.add_argument('--num-layers', type=int, default=2)
    parser.add_argument('--hidden-dims', type=int, default=16)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--device', type=str, default='cpu')  # 'cpu' or 'cuda:N'
    parser.add_argument('--num-epochs', type=int, default=1)
    parser.add_argument('--batches-per-epoch', type=int, default=10000)
    parser.add_argument('--num-workers', type=int, default=0)
    parser.add_argument('--lr', type=float, default=3e-5)
    parser.add_argument('--eval-epochs', type=int, default=0)
    parser.add_argument('--save-epochs', type=int, default=0)
    parser.add_argument('--retrain', type=int, default=0)
    parser.add_argument('-k', type=int, default=10)
    args = parser.parse_args()
    data_path = 'C:\\Users\\Juchan\\Desktop\\Recomennder system\\output'
    save_path = 'C:\\Users\\Juchan\\Desktop\\Recomennder system\\result\\final'
    # Load dataset
    with open(data_path, 'rb') as f:
        dataset = pickle.load(f)
        
    data_dict = {
        'graph': dataset['train-graph'],
        'val_matrix': None,
        'test_matrix': None,
        'item_texts': dataset['item-texts'],
        'testset': dataset['testset'], 
        'paper_ntype': dataset['paper-type'],
        'author_ntype': dataset['author-type'],
        'paper_to_author_etype': dataset['paper-to-author-type'],
        'timestamp': dataset['timestamp-edge-column'],
        'paper_category': dataset['paper-category'], 
        'author_category': dataset['author-category']
    }
    
    # Training
    gnn, epoch, opt, loss = train(data_dict, args)


#    torch.save({
#                'epoch': epoch,
#                'model_state_dict': gnn.state_dict(),
#               'optimizer_state_dict': opt.state_dict(),
#                'loss': loss
#            }, args.save_path + '_' + str(epoch) + 'epoch.pt')
    
    # 모델 및 최적화기 상태를 딕셔너리에 저장
    state = {
            'epoch': epoch,
            'model_state_dict': gnn.state_dict(),
            'optimizer_state_dict': opt.state_dict(),
            'loss': loss
            }

    #  .pkl 형식으로 파일 저장
    with open(save_path + '_' + str(epoch) + 'epoch.pkl', 'wb') as f:
        pickle.dump(state, f)