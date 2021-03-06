import argparse
from utils import seed_everything, VQAMed, train_one_epoch, validate, test, load_data, LabelSmoothing
import wandb
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset
import torch.optim as optim
import torch.optim.lr_scheduler as lr_scheduler
from torchvision import transforms, models
from torch.cuda.amp import GradScaler
#from torchtoolbox.transform import Cutout
import os
#import pytorch_lightning as pl
import warnings
from models.mmbert import Model

warnings.simplefilter("ignore", UserWarning)



if __name__ == '__main__':

    parser = argparse.ArgumentParser(description = "Evaluate")

    parser.add_argument('--run_name', type = str, required = True, help = "run name for wandb")
    parser.add_argument('--data_dir', type = str, required = False, default = "../ImageClef-2019-VQA-Med", help = "path for data")
    parser.add_argument('--model_dir', type = str, required = False, default = "../ImageClef-2019-VQA-Med/mmbert/MLM/vqamed-roco-1_acc.pt", help = "path to load weights")
    parser.add_argument('--save_dir', type = str, required = False, default = "../ImageClef-2019-VQA-Med/mmbert", help = "path to save weights")
    parser.add_argument('--category', type = str, required = False, default = None,  help = "choose specific category if you want")
    parser.add_argument('--use_pretrained', action = 'store_true', default = False, help = "use pretrained weights or not")
    parser.add_argument('--mixed_precision', action = 'store_true', default = False, help = "use mixed precision or not")
    parser.add_argument('--clip', action = 'store_true', default = False, help = "clip the gradients or not")

    parser.add_argument('--seed', type = int, required = False, default = 42, help = "set seed for reproducibility")
    parser.add_argument('--num_workers', type = int, required = False, default = 4, help = "number of workers")
    parser.add_argument('--epochs', type = int, required = False, default = 100, help = "num epochs to train")
    parser.add_argument('--train_pct', type = float, required = False, default = 1.0, help = "fraction of train samples to select")
    parser.add_argument('--valid_pct', type = float, required = False, default = 1.0, help = "fraction of validation samples to select")
    parser.add_argument('--test_pct', type = float, required = False, default = 1.0, help = "fraction of test samples to select")

    parser.add_argument('--max_position_embeddings', type = int, required = False, default = 28, help = "max length of sequence")
    parser.add_argument('--batch_size', type = int, required = False, default = 16, help = "batch size")
    parser.add_argument('--lr', type = float, required = False, default = 1e-4, help = "learning rate'")
    # parser.add_argument('--weight_decay', type = float, required = False, default = 1e-2, help = " weight decay for gradients")
    parser.add_argument('--factor', type = float, required = False, default = 0.1, help = "factor for rlp")
    parser.add_argument('--patience', type = int, required = False, default = 10, help = "patience for rlp")
    # parser.add_argument('--lr_min', type = float, required = False, default = 1e-6, help = "minimum lr for Cosine Annealing")
    parser.add_argument('--hidden_dropout_prob', type = float, required = False, default = 0.3, help = "hidden dropout probability")
    parser.add_argument('--smoothing', type = float, required = False, default = None, help = "label smoothing")

    parser.add_argument('--image_size', type = int, required = False, default = 224, help = "image size")
    parser.add_argument('--hidden_size', type = int, required = False, default = 312, help = "hidden size")
    parser.add_argument('--vocab_size', type = int, required = False, default = 30522, help = "vocab size")
    parser.add_argument('--type_vocab_size', type = int, required = False, default = 2, help = "type vocab size")
    parser.add_argument('--heads', type = int, required = False, default = 12, help = "heads")
    parser.add_argument('--n_layers', type = int, required = False, default = 4, help = "num of layers")
    parser.add_argument('--num_vis', type = int, required = True, help = "num of visual embeddings")
    parser.add_argument('--task', type=str, default='MLM',
                        choices=['MLM', 'distillation'], help='task which the model was pre-trained on')
    parser.add_argument('--clinicalbert', type=str, default='emilyalsentzer/Bio_ClinicalBERT')
    parser.add_argument('--dataset', type=str, default='VQA-Med', help='roco or vqamed2019')
    parser.add_argument('--cnn_encoder', type=str, default='resnet152', help='name of the cnn encoder')
    parser.add_argument('--use_relu', action = 'store_true', default = False, help = "use ReLu")
    parser.add_argument('--transformer_model', type=str, default='transformer',choices=['transformer', 'realformer', 'feedback-transformer'], help='name of the transformer model')

    args = parser.parse_args()
    
    model_name = args.model_dir.split('/')[-1]
    wandb.init(project='medvqa', name = 'testing-'+model_name, config = args) #args.run_name

    seed_everything(args.seed)


    train_df, val_df, test_df = load_data(args)


    if args.category:
            
        train_df = train_df[train_df['category']==args.category].reset_index(drop=True)
        val_df = val_df[val_df['category']==args.category].reset_index(drop=True)
        test_df = test_df[test_df['category']==args.category].reset_index(drop=True)


    df = pd.concat([train_df, val_df, test_df]).reset_index(drop=True)

    ans2idx = {ans:idx for idx,ans in enumerate(df['answer'].unique())}
    idx2ans = {idx:ans for ans,idx in ans2idx.items()}




    df['answer'] = df['answer'].map(ans2idx).astype(int)
    train_df = df[df['mode']=='train'].reset_index(drop=True)
    val_df = df[df['mode']=='val'].reset_index(drop=True)
    test_df = df[df['mode']=='test'].reset_index(drop=True)

    num_classes = len(ans2idx)

    args.num_classes = num_classes

    train_df = pd.concat([train_df, val_df]).reset_index(drop=True)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'

    model = Model(args)

    model.classifier[2] = nn.Linear(args.hidden_size, num_classes)

    print('Loading model at ', args.model_dir)
    model.load_state_dict(torch.load(args.model_dir))

        
    model.to(device)

    wandb.watch(model, log='all')


    optimizer = optim.Adam(model.parameters(),lr=args.lr)
    scheduler = lr_scheduler.ReduceLROnPlateau(optimizer, patience = args.patience, factor = args.factor, verbose = True)


    if args.smoothing:
        criterion = LabelSmoothing(smoothing=args.smoothing)
    else:
        criterion = nn.CrossEntropyLoss()

    scaler = GradScaler()


    test_tfm = transforms.Compose([transforms.Resize(224), #added with profs
                                   transforms.CenterCrop(224), #added with profstransforms.ToTensor(),
                                   transforms.ToTensor(), 
                                   transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])



    testdataset = VQAMed(test_df, imgsize = args.image_size, tfm = test_tfm, args = args, mode='test')

    testloader = DataLoader(testdataset, batch_size = args.batch_size, shuffle=False, num_workers = args.num_workers)

    best_acc1 = 0
    best_acc2 = 0
    best_loss = np.inf
    counter = 0

    test_loss, predictions, acc, bleu = test(testloader, model, criterion, device, scaler, args, test_df,idx2ans)

    wandb.log({
                'test_loss': test_loss,
                'learning_rate': optimizer.param_groups[0]["lr"],

                'total_bleu':    bleu['total_bleu'],
                'binary_bleu':   bleu['binary_bleu'],
                'plane_bleu':    bleu['plane_bleu'],
                'organ_bleu':    bleu['organ_bleu'],
                'modality_bleu': bleu['modality_bleu'],
                'abnorm_bleu':   bleu['abnorm_bleu'],

                'total_acc':    acc['total_acc'],
                'binary_acc':   acc['binary_acc'],
                'plane_acc':    acc['plane_acc'],
                'organ_acc':    acc['organ_acc'],
                'modality_acc': acc['modality_acc'],
                'abnorm_acc':   acc['abnorm_acc']
                
            })

    
    test_df['preds'] = predictions
    test_df['decode_preds'] = test_df['preds'].map(idx2ans)
    test_df['decode_ans'] = test_df['answer'].map(idx2ans)
    test_df.to_csv(f'../ImageClef-2019-VQA-Med/mmbert/{model_name}_preds.csv', index = False)
    
    result = test_df[['img_id', 'decode_preds']]
    result['img_id'] = result['img_id'].apply(lambda x: x.split('/')[-1].split('.')[0])
    result.to_csv(f'../ImageClef-2019-VQA-Med/mmbert/{model_name}_res.txt', index = False, header=False, sep='|')
    print('acc', acc)
    print('bleu', bleu)

            