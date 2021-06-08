import argparse
import os
import random
from typing import List

import numpy as np
import torch.nn as nn

from data_sets import TranslationDataSet
from models import EncoderVanilla, DecoderVanilla, Seq2Seq
from vocab import Vocab
import torch
from torch.utils.data import DataLoader, Dataset
from torch import optim
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from torch.nn.utils.rnn import pad_sequence
import sacrebleu


def set_seed(seed):
    os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available() == 'cuda':
        torch.cuda.manual_seed_all(seed)


def pad_collate(batch):
    (ss, tt) = zip(*batch)
    source_lens = [len(sent) for sent in ss]
    target_lens = [len(sent) for sent in tt]

    xx_pad = pad_sequence(ss, batch_first=True, padding_value=0)
    yy_pad = pad_sequence(tt, batch_first=True, padding_value=0)

    return xx_pad, yy_pad, source_lens, target_lens


class Trainer:
    def __init__(self, model: nn.Module, train_data: Dataset, dev_data: Dataset, source_vocab: Vocab,
                 target_vocab: Vocab,train_batch_size=1, lr=0.002, part=None, output_path=None):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.part = part
        self.model = model
        self.dev_batch_size = 1
        self.n_epochs = 10
        self.source_vocab = source_vocab
        self.target_vocab = target_vocab
        self.train_data = DataLoader(train_data, batch_size=train_batch_size, collate_fn=pad_collate)
        self.dev_data = DataLoader(dev_data, batch_size=self.dev_batch_size,  collate_fn=pad_collate)
        self.optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
        self.loss_func = nn.CrossEntropyLoss(ignore_index=self.target_vocab.PAD_IDX)
        self.model.to(self.device)
        self.model_args = {"part": self.part, "lr": lr,"batch_size": train_batch_size,
                           "hidden_dim": self.model.encoder.hidden_size}
        if output_path is None:
            output_path = self.suffix_run()

        self.saved_model_path = f"{output_path}.bin"
        self.writer = SummaryWriter(log_dir=f"tensor_board/{output_path}")
        self.best_model = None
        self.best_score = 0

    def train(self):
        num_samples=0
        for epoch in range(self.n_epochs):
            print(f"start epoch: {epoch + 1}")
            train_loss = 0.0
            step_loss = 0
            self.model.train()  # prep model for training
            for step, (source, target, source_lens, target_lens) in tqdm(enumerate(self.train_data), total=len(self.train_data)):
                num_samples += self.train_data.batch_size
                source = source.to(self.device)
                target = target.to(self.device)
                # clear the gradients of all optimized variables
                self.optimizer.zero_grad()
                self.model.zero_grad()
                # forward pass: compute predicted outputs by passing inputs to the model
                output = self.model(source, target, train=True)  # Eemnded Data Tensor size (1,5)
                output_dim = output.shape[-1]
                output = output.view(-1, output_dim)
                output = output[1:]
                target = target[:,1:]
                # calculate the loss
                loss = self.loss_func(output, target.view(-1))
                # backward pass: compute gradient of the loss with respect to model parameters
                loss.backward()
                # print(self.model.decoder.out_linear.weight)
                # perform a single optimization step (parameter update)
                self.optimizer.step()
                # update running training loss
                train_loss += loss.item() * target.size(0)
                step_loss += loss.item() * target.size(0)
                # if num_samples >= self.steps_to_eval:
                #     num_samples = 0
                #     print(f"in step: {(step+1)*self.train_data.batch_size} train loss: {step_loss}")
                #     self.writer.add_scalar('Loss/train_step', step_loss, step * (epoch + 1))
                #     step_loss = 0.0
                #     # print((step+1)*self.train_data.batch_size + epoch * len(self.train_data))
                #     self.evaluate_model((step+1)*self.train_data.batch_size + epoch * len(self.train_data), "step", self.dev_data) TODO delete
            print(f"in epoch: {epoch + 1} train loss: {train_loss}")
            self.writer.add_scalar('Loss/train', train_loss, epoch+1)
            print((epoch+1) * len(self.train_data) * self.train_data.batch_size)
            self.evaluate_model((epoch+1) * len(self.train_data)*self.train_data.batch_size, "epoch", self.dev_data)

    def evaluate_model(self, step, stage, data_set,write=True):
        with torch.no_grad():
            self.model.eval()
            loss = 0

            prediction = []
            all_target = []
            for eval_step, (source, target, source_lens, target_lens) in tqdm(enumerate(data_set), total=len(data_set),
                                                                          desc=f"dev step {step} loop"):
                source = source.to(self.device)
                target = target.to(self.device)
                output = self.model(source, target, train=False)
                output_dim = output.shape[-1]

                loss = self.loss_func(output.view(-1, output_dim),
                                      target.view(-1))
                loss += loss.item()
                predicted = torch.argmax(output.view(-1, output_dim), dim=1)
                prediction.append(predicted.view(-1).tolist())
                all_target.append(target.view(-1).tolist())

            bleu_score = self.bleu_score(prediction, all_target)
            if write:
                print(f'bleu_score/dev_{stage}: {bleu_score}')

                self.writer.add_scalar(f'bleu_score/dev_{stage}', bleu_score, step)
                self.writer.add_scalar(f'Loss/dev_{stage}', loss, step)
                if bleu_score > self.best_score:
                    self.best_score = bleu_score
                    print("best score: ", self.best_score)
                    torch.save(self.model.state_dict(), self.saved_model_path)

            else:
                print(f'Accuracy/train_{stage}: {bleu_score}')


        self.model.train()

    def suffix_run(self):
        res = ""
        for k, v in self.model_args.items():
            res += f"{k}_{v}_"
        res = res.strip("_")
        return res

    def test(self, test_df):
        test = DataLoader(test_df, batch_size=self.dev_batch_size,  collate_fn=pad_collate)
        self.model.load_state_dict(torch.load(self.saved_model_path))
        self.model.eval()
        prediction = []
        for eval_step, (data, _, data_lens, _) in tqdm(enumerate(test), total=len(test),
                                                       desc=f"test data"):
            data = data.to(self.device)
            output = self.model(data, data_lens)
            _, predicted = torch.max(output, 1)
            prediction += predicted.tolist()
        return [self.vocab.i2label[i] for i in prediction]

    def bleu_score(self, predict: List, target: List):
        predict, target = self.get_unpadded_samples(predict, target)
        bleu = sacrebleu.corpus_bleu(predict,target)
        return bleu.score

    def get_unpadded_samples(self, predict, target):
        no_pad_predict = []
        no_pad_target = []

        for p_sen, t_sen in zip(predict, target):
            unpad_t = []
            unpad_p = []
            for p, t in zip(p_sen, t_sen):
                if t == 0:
                    continue
                unpad_t.append(self.target_vocab.i2token[t])
                unpad_p.append(self.target_vocab.i2token[p])
            no_pad_predict.append(" ".join(unpad_p))
            no_pad_target.append(" ".join(unpad_t))
        return no_pad_predict, [no_pad_target]

    def dump_test_file(self, test_prediction, test_file_path): #TODO
        res = []
        cur_i = 0
        with open(test_file_path) as f:
            lines = f.readlines()
        for line in lines:
            if line == "" or line == "\n":
                res.append(line)
            else:
                pred = f"{line.strip()} {test_prediction[cur_i]}\n"
                res.append(pred)
                cur_i += 1
        pred_path = f"{self.suffix_run()}.tsv"
        with open(pred_path, mode='w') as f:
            f.writelines(res)

def parse_arguments():
    p = argparse.ArgumentParser(description='Hyperparams')
    p.add_argument('-batch_size', type=int, default=32,
                   help='number of epochs for train')
    p.add_argument('-lr', type=float, default=0.0001,
                   help='initial learning rate')
    p.add_argument('-grad_clip', type=float, default=10.0,
                   help='in case of gradient explosion')
    return p.parse_args()


def main():
    args = parse_arguments()
    hidden_size = 256
    embed_size = 128
    data_path = "data/{}.{}"

    source_vocab = Vocab(data_path.format("train", "src"))
    target_vocab = Vocab(data_path.format("train", "trg"))
    encoder = EncoderVanilla(vocab_size=source_vocab.vocab_size, embed_size=embed_size, hidden_size=hidden_size,
                      n_layers=3, dropout=0.5)
    decoder = DecoderVanilla(vocab_size= target_vocab.vocab_size,embed_size=embed_size, hidden_size=hidden_size,
                      n_layers=3, dropout=0.5)
    model = Seq2Seq(encoder, decoder)
    print(model)
    train_df = TranslationDataSet(source=data_path.format("train", "src"),
                                  target=data_path.format("train", "trg"),
                                  source_vocab=source_vocab,
                                  target_vocab=target_vocab
                                  )
    dev_df = TranslationDataSet(source=data_path.format("dev", "src"),
                                target=data_path.format("dev", "trg"),
                                source_vocab=source_vocab,
                                target_vocab=target_vocab
                                )

    trainer = Trainer(model=model,
                      train_data=train_df,
                      dev_data=dev_df,
                      source_vocab=source_vocab,
                      target_vocab=target_vocab)
    trainer.train()

if __name__ == '__main__':
    main()