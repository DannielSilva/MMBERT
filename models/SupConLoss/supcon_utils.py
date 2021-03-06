"""
Original Author: Yonglong Tian (yonglong@mit.edu)
Date: May 07, 2020
"""
"""
Adapted to use from 
https://github.com/HobbitLong/SupContrast
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from tqdm import tqdm
import numpy as np
import random
from transformers import BertTokenizer, BertModel, AutoTokenizer, AutoModel, logging
from sentence_transformers import SentenceTransformer, util
from googletrans import Translator
import os
from PIL import Image
from roco_utils import encode_text
from torch.utils.data import Dataset, DataLoader

from bert_score import BERTScorer


class TwoCropTransform:
    """Create two crops of the same image"""
    def __init__(self, transform):
        self.transform = transform

    def __call__(self, x):
        return [self.transform(x), self.transform(x)]

model_dict = {
    'resnet18':  512,
    'resnet34':  512,
    'resnet50':  2048,
    'resnet101': 2048,
    'resnet152': 2048,
    'efficientnet-b3': 1536,
    'efficientnet-b5': 2048,
    'tf_efficientnetv2_m': 1280
}

class SupConEncoder(nn.Module):
    """backbone + projection head"""
    def __init__(self, name='resnet152', head='mlp', feat_dim=128):
        super(SupConEncoder, self).__init__()
        dim_in = model_dict[name]
        #self.encoder = encoder
        self.name = name
        self.gap = nn.AdaptiveAvgPool2d((1,1))
        if head == 'linear':
            self.head = nn.Linear(dim_in, feat_dim)
        elif head == 'mlp':
            self.head = nn.Sequential(
                nn.Linear(dim_in, dim_in),
                nn.ReLU(inplace=True),
                nn.Linear(dim_in, feat_dim)
            )
        else:
            raise NotImplementedError(
                'head not supported: {}'.format(head))

    def forward(self, x):
        feat = self.features(x)
        feat = F.normalize(self.head(feat), dim=1)
        return feat

    def add_encoder(self, encoder):
        self.encoder = encoder
        if 'resnet' in self.name:
            self.encoder.fc = nn.Sequential()

    def features(self,x):
        if 'resnet' in self.name:
            return self.encoder(x)
        elif 'efficientnetv2' in self.name:
            return self.gap(self.encoder.forward_features(x)).squeeze()
        elif 'efficientnet' in self.name:
            return self.gap(self.encoder.extract_features(x)).squeeze()

def get_supcon_model(args):
    return SupConEncoder(name=args.cnn_encoder)

class SimilarityCalculator(nn.Module):

    def __init__(self, args, device):
        super().__init__()
        self.similarity = args.similarity
        print('Similarity', self.similarity)
        if args.similarity == 'cosine':
            logging.set_verbosity_error()
            self.tokenizer = AutoTokenizer.from_pretrained(args.clinicalbert, model_max_length=args.max_token_length)
            self.model = AutoModel.from_pretrained(args.clinicalbert)
            self.device = device
            self.cos = nn.CosineSimilarity(dim=1, eps=1e-6)
            # self.layer = nn.Linear(768, 768)
            # self.w = self.layer.weight.clone().detach()
        elif args.similarity == 'sentence_transformers':
            #name='all-MiniLM-L6-v2'
            self.model = SentenceTransformer('all-mpnet-base-v2')
        elif args.similarity == 'bert_score':
            print('Using bert score', args.bert_score)
            if args.bert_score == 'bert':
                self.scorer = BERTScorer(lang="en", rescale_with_baseline=True)
            elif args.bert_score == 'scibert':
                self.scorer = BERTScorer(lang="en",model_type='allenai/scibert_scivocab_uncased')

    def jaccard(self,caption,aug,bsz):
        mask = torch.zeros(bsz, bsz, dtype=torch.float)
        for c1 in range(len(caption)):
            for c2 in range(len(aug)):
                if c1 != c2:
                    mask[c1,c2] = self.jaccard_similarity(caption[c1],aug[c2])
                else:
                    mask[c1,c2] = 1.0
        return mask

    def jaccard_similarity(self,doc1, doc2): 
        
        # List the unique words in a document
        words_doc1 = set(doc1.lower().split()) 
        words_doc2 = set(doc2.lower().split())
        
        # Find the intersection of words list of doc1 & doc2
        intersection = words_doc1.intersection(words_doc2)

        # Find the union of words list of doc1 & doc2
        union = words_doc1.union(words_doc2)
            
        # Calculate Jaccard similarity score 
        # using length of intersection set divided by length of union set
        if len(union) != 0:
            return float(len(intersection)) / len(union)
        else:
            print('union == 0\n1st: ', doc1,'\n2nd: ',doc2)
            return 0.0

    def bert_embedd(self,doc1,doc2, bsz):
        encoded_input = self.tokenizer(list(doc1)+list(doc2), return_tensors='pt',truncation=True, padding=True).to(self.device)
        self.model.eval()
        with torch.no_grad():
            output = self.model(**encoded_input).last_hidden_state
            f1, f2 = torch.split(output, [bsz, bsz], dim=0)

            f1, f2 = f1.mean(1), f2.mean(1)                                 #similarity between mean of the embeddings of each sentene
        # f1, f2 = self.layer(f1), self.layer(f2)
        # print("torch.eq", torch.eq(self.w, self.layer.weight))
        #self.w = self.layer.weight
        
            a_n, b_n = f1.norm(dim=1)[:, None], f2.norm(dim=1)[:, None]
            eps = 1e-8                                                      #eps for num stability
            # Given that cos_sim(u, v) = dot(u, v) / (norm(u) * norm(v))
            #                          = dot(u / norm(u), v / norm(v))
            a_norm = f1 / torch.max(a_n, torch.tensor([eps]).to(self.device))               #norm rows
            b_norm = f2 / torch.max(b_n, torch.tensor([eps]).to(self.device))
            sim_mt = torch.mm(a_norm, b_norm.transpose(0, 1))               #dot product with transpose
            return sim_mt.fill_diagonal_(1)                                 #1 in diagonal for the mask


    def sentence_trans(self,doc1,doc2,bsz):
        with torch.no_grad():
            emb1 = self.model.encode(doc1)
            emb2 = self.model.encode(doc2)

            #assert da shape com bsz
            return util.cos_sim(emb1, emb2).fill_diagonal_(1)

    def bert_score(self,caption,aug,bsz):
        mask = torch.zeros(bsz, bsz, dtype=torch.float)
        for c1 in range(len(caption)):
            for c2 in range(len(aug)):
                if c1 != c2:
                    mask[c1,c2] = self.calc_bert_score(caption[c1],aug[c2])
                else:
                    mask[c1,c2] = 1.0
        return mask

    def calc_bert_score(self,doc1, doc2):
        _,_, F1 = self.scorer.score([doc1], [doc2]) #P, R, F1
        return F1.item()

    def forward(self,doc1,doc2,bsz):
        if self.similarity == 'cosine':
            return self.bert_embedd(doc1,doc2,bsz)
        elif self.similarity == 'jaccard':
            return self.jaccard(doc1,doc2,bsz)
        elif self.similarity == 'sentence_transformers':
            return self.sentence_trans(doc1,doc2, bsz)
        elif self.similarity == 'bert_score':
            return self.bert_score(doc1,doc2, bsz)


def buildMask(bsz,caption, aug, args, sim_calculator):
    if args.con_task == 'simclr':
        return None
    mask = sim_calculator(caption,aug,bsz)    
    return mask

class ROCO_SupCon(Dataset):
    def __init__(self, args, df, tfm, keys, mode):
        self.df = df.values
        self.args = args
        self.path = args.data_dir
        self.tfm = tfm
        self.keys = keys
        self.mode = mode

        self.translator = Translator()
        self.tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

        self.clinicalbert = None
        
        
    def __len__(self):
        return len(self.df)

    def __getitem__(self, idx):
        name = self.df[idx,1] 
        path = os.path.join(self.path, self.mode, 'radiology', 'images',name)

        img = Image.open(path).convert('RGB')
        
        if self.tfm:
            img = self.tfm(img)
    
        caption = self.df[idx, 2].strip()       
        tokens, segment_ids, input_mask, targets = encode_text(caption, self.tokenizer, self.keys, self.args, self.clinicalbert)

        aug_caption = self.get_translation(idx)    #self.translate_caption(caption)
        aug_tokens, _, _, aug_targets = encode_text(aug_caption, self.tokenizer, self.keys, self.args, self.clinicalbert)
        

        return img, tokens, aug_tokens, segment_ids, input_mask, targets, aug_targets, caption, aug_caption

    def get_translation(self, idx):
        #get translation from table in columns from 3 to 5 inclusive

        # table columns
        # 0     1        2    3  4  5
        # id img_name caption fr de es
        i = random.randint(3,5)
        return self.df[idx, i].strip()  
        
    def translate_caption(self, caption):
        langs = ['fr','de','pt']
        l = random.choice(langs)
        result = self.translator.translate(caption, src='en', dest=l)
        final = self.translator.translate(result.text, src=l, dest='en')
        return final.text

def process_tensors(img,caption_token,aug_tokens,segment_ids,attention_mask,target,aug_targets):
    def cat_tensors(a,b):
        return torch.cat([a,b], dim=0)
    return cat_tensors(img[0],img[1]),cat_tensors(caption_token,aug_tokens),cat_tensors(segment_ids,segment_ids),cat_tensors(attention_mask,attention_mask),cat_tensors(target,aug_targets)


def split_feat(feat,bsz):
    f1, f2 = torch.split(feat, [bsz, bsz], dim=0) # (bs//2 x feat_dim), (bs//2 x feat_dim)
    return torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1) # (bs//2, 2, feat_dim) 

def train_one_epoch(loader, model, criterion, supcon_loss, optimizer, device, args, epoch, sim_calculator):

    model.train()
    train_loss = []
    PREDS = []
    TARGETS = []
    bar = tqdm(loader, leave=False)
    for i, (img, caption_token,aug_tokens,segment_ids,attention_mask,target,aug_targets,caption_text,aug_text) in enumerate(bar):
        img,caption_token,segment_ids,attention_mask,target = process_tensors(img,caption_token,aug_tokens,segment_ids,attention_mask,target,aug_targets)
        img, caption_token,segment_ids,attention_mask,target = img.to(device), caption_token.to(device), segment_ids.to(device), attention_mask.to(device), target.to(device)
        
        caption_token = caption_token.squeeze(1)
        attention_mask = attention_mask.squeeze(1)
    
        loss_func = criterion
        optimizer.zero_grad()
        
        logits, feat = model(img, caption_token, segment_ids, attention_mask) # (bs x seq_len x vocab_size) , (bs x feat_dim)
        logits = logits.log_softmax(-1)  # (bs x seq_len x vocab_size)
        loss = loss_func(logits.permute(0,2,1), target)  
        
        bsz = img.shape[0] //2 #2 = n_views
        feat = split_feat(feat,bsz) 
        mask = buildMask(bsz,caption_text, aug_text, args, sim_calculator) #mask=None if simclr else mask built with [jaccard,cosine,sentence_transformers] similarity for supcon
        loss_supcon = supcon_loss(feat)#supcon_loss(features, mask=mask)

        loss = loss + loss_supcon

        # print('e')
        loss.backward()
        #import IPython; IPython.embed(); import sys; sys.exit(0)
        optimizer.step()    


        bool_label = target > 0
        acc = 0.0
        if bool_label.any():
            pred = logits[bool_label, :].argmax(1)
            valid_labels = target[bool_label]   
            
            PREDS.append(pred)
            TARGETS.append(valid_labels)
            
            acc = (pred == valid_labels).type(torch.float).mean() * 100.

        loss_np = loss.detach().cpu().numpy()
        train_loss.append(loss_np)
        
        
        bar.set_description('train_loss: %.5f, train_acc: %.2f' % (loss_np, acc))
        

        
    PREDS = torch.cat(PREDS).cpu().numpy()
    TARGETS = torch.cat(TARGETS).cpu().numpy()

#     # Calculate total accuracy
    total_acc = (PREDS == TARGETS).mean() * 100.


    return np.mean(train_loss), total_acc


def validate(loader, model,criterion, scaler, device, args, epoch):

    model.eval()
    val_loss = []

    PREDS = []
    TARGETS = []
    bar = tqdm(loader, leave=False)

    with torch.no_grad():
        for i, (img, caption_token,segment_ids,attention_mask,target) in enumerate(bar):

            img, caption_token,segment_ids,attention_mask,target = img.to(device), caption_token.to(device), segment_ids.to(device), attention_mask.to(device), target.to(device)
            caption_token = caption_token.squeeze(1)
            attention_mask = attention_mask.squeeze(1)
            
            loss_func = criterion

            
            logits, _ = model(img, caption_token, segment_ids, attention_mask)
            
            logits = logits.log_softmax(-1)  # (bs x seq_len x vocab_size)
            loss = loss_func(logits.permute(0,2,1), target)
                    

            
            bool_label = target > 0
            acc = 0.0
            if bool_label.any():
                pred = logits[bool_label, :].argmax(1)
                valid_labels = target[bool_label]   
            
                PREDS.append(pred)
                TARGETS.append(valid_labels)
            
                acc = (pred == valid_labels).type(torch.float).mean() * 100.

            loss_np = loss.detach().cpu().numpy()

            val_loss.append(loss_np)

            bar.set_description('val_loss: %.5f, val_acc: %.5f' % (loss_np, acc))
           

        val_loss = np.mean(val_loss)

    
    PREDS = torch.cat(PREDS).cpu().numpy()
    TARGETS = torch.cat(TARGETS).cpu().numpy()

    # Calculate total accuracy
    total_acc = (PREDS == TARGETS).mean() * 100.

    return val_loss, PREDS, total_acc




'''vvv this method used old supcon encoder vvv'''

def train_one_epoch_old(loader, model, supcon_model, criterion, supcon_loss, optimizer, device, args, epoch):

    model.train()
    train_loss = []
    PREDS = []
    TARGETS = []
    bar = tqdm(loader, leave=False)
    for i, (img, caption_token,aug_tokens,segment_ids,attention_mask,target,aug_targets,caption_text,aug_text) in enumerate(bar):
        img,caption_token,segment_ids,attention_mask,target = process_tensors(img,caption_token,aug_tokens,segment_ids,attention_mask,target,aug_targets)
        img, caption_token,segment_ids,attention_mask,target = img.to(device), caption_token.to(device), segment_ids.to(device), attention_mask.to(device), target.to(device)
        
        caption_token = caption_token.squeeze(1)
        attention_mask = attention_mask.squeeze(1)
    
        loss_func = criterion
        optimizer.zero_grad()

        
        logits = model(img, caption_token, segment_ids, attention_mask)
        logits = logits.log_softmax(-1)  # (bs x seq_len x vocab_size)
        loss = loss_func(logits.permute(0,2,1), target)  
        
        bsz = img.shape[0] //2
        features = supcon_model(img) # (bs x feat_dim)
        f1, f2 = torch.split(features, [bsz, bsz], dim=0) # (bs//2 x feat_dim), (bs//2 x feat_dim)
        features = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1) # (bs//2, 2, feat_dim)

        mask = buildMask(bsz,caption_text, aug_text)
        loss_supcon = supcon_loss(features, mask=mask)

        loss = loss + loss_supcon

        loss.backward()
        optimizer.step()    

           
    
        bool_label = target > 0
        acc = 0.0
        if bool_label.any():
            pred = logits[bool_label, :].argmax(1)
            valid_labels = target[bool_label]   
            
            PREDS.append(pred)
            TARGETS.append(valid_labels)
            
            acc = (pred == valid_labels).type(torch.float).mean() * 100.

        loss_np = loss.detach().cpu().numpy()
        train_loss.append(loss_np)
        
        
        bar.set_description('train_loss: %.5f, train_acc: %.2f' % (loss_np, acc))
        

        
    PREDS = torch.cat(PREDS).cpu().numpy()
    TARGETS = torch.cat(TARGETS).cpu().numpy()

#     # Calculate total accuracy
    total_acc = (PREDS == TARGETS).mean() * 100.


    return np.mean(train_loss), total_acc