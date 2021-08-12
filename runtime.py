import os
import sys
import gc
import math
import threading
import psutil
import requests
import time
import torch
from math import floor
import numpy as np
from PIL import Image
import torch.nn as nn
import torch.distributed.autograd as dist_autograd
import torch.distributed.rpc as rpc
from torch.distributed.rpc.api import _delete_all_user_and_unforked_owner_rrefs
from torch.distributed.rpc import RRef, _get_debug_info, _rref_context_get_debug_info
from torch.nn import functional as F
from transformers import AutoConfig, ViTFeatureExtractor, ViTForImageClassification, ViTModel
from transformers.models.vit.modeling_vit import ViTEmbeddings, ViTLayer, ViTSelfAttention, ViTSelfOutput,ViTIntermediate, ViTOutput

#########################################################
#                 Check Enviroment Settings             #
#########################################################

## Force pytorch use CPU
# torch.set_printoptions(profile="full")
device = torch.device('cpu')
MASTER_ADDR = '127.0.0.1' #'10.52.3.175' #'127.0.0.1' # '172.30.0.21'
MASTER_PORT = '29501'
SOCKET_IFNAME = "lo0"
# parallel_threads = 2
# torch.set_num_threads(parallel_threads)
# torch.set_num_interop_threads(parallel_threads)
torch.set_grad_enabled(False)
print(f"Use device: {device},  # parallel intra nodes threads: {torch.get_num_threads()}, # parallel inter nodes threads: {torch.get_num_interop_threads()}")
process = psutil.Process(os.getpid())
#########################################################
#                 Configuration for Network             #
#########################################################

# *****  Define the World Size and partition Method ******#

# 'google/vit-base-patch16-224'
# 'google/vit-large-patch16-224'
# 'google/vit-huge-patch14-224-in21k'
model_name= 'google/vit-base-patch16-224'
total_rank = 2
partition = [1, 24,  25,48] #[1,8, 9,13, 14,27, 28,41, 42,44, 45,48] #[1,17, 18,25, 26,37, 38,41, 42,45, 46,48] #[1,24, 25,48] #[1,24, 25,42, 43,44, 45,48]   
num_batches = 1
batch_size = 1
num_worker_threads = 32
splits = [1]
operators_list = ["LayerNorm + Attention", "Attention Output + residuel Connection", "LayerNorm + MLP-1", "MLP-2 + residuel Connection"]
## random data
# img = torch.randn(3, 384, 384)
## ground truth: Egyptian cat
url = 'http://images.cocodataset.org/val2017/000000039769.jpg'
image = Image.open(requests.get(url, stream=True).raw)
# image = Image.open('./images/panda.jpeg')
imgs = [image for i in range(batch_size)]

# ***********************  End  **************************#

#########################################################
#           Define Model Parallel Transformer           #
#########################################################
class TransformerBase(nn.Module):
    def __init__(self, rank, model_name, is_first, is_last, start_layer, end_layer, load_weight=True):
        super(TransformerBase, self).__init__()
        self.model_name = model_name
        self.config = AutoConfig.from_pretrained(model_name)
        print(f">>>> Model name {model_name}")
        self.rank = rank
        self.is_first = is_first
        self.is_last = is_last
        self.embeddings = None
        self.layernorm = None
        self.classifier = None
        self.has_first_ununit = False
        self.has_last_ununit = False
        self.has_mid_unit = False
        self.current_layer_idx = start_layer  ## the next layer to load
        self.start_layer = start_layer
        self.end_layer = end_layer

        self.load_weight = load_weight
        self._lock = threading.Lock()
        self.total_time = 0
        self.total_batch = 0

        ## quantization observer 
        self.observer = torch.quantization.HistogramObserver(dtype=torch.qint8,qscheme=torch.per_channel_affine)
        self.x_scale = 0.1
        self.s_scale = 0.1
        self.x_zp = 0
        self.s_zp = 0

        ## operations/transformer layers set
        self.first_ops = nn.ModuleList()
        self.vit_layers = nn.ModuleList()
        self.last_ops = nn.ModuleList()

        ## weight file anme
        if self.model_name == 'google/vit-base-patch16-224':
            self.weights_file_name = 'ViT-B_16-224.npz'
        elif self.model_name == 'google/vit-large-patch16-224':
            self.weights_file_name = 'ViT-L_16-224.npz'
        elif self.model_name == 'google/vit-huge-patch14-224-in21k':
            self.weights_file_name = 'ViT-H_14.npz'
        if self.load_weight:
            print(f">>>> Load weight file f{self.weights_file_name}")
        self._make_layer()
        print(f"======= Finish Build TransformerShard{self.rank} ==========")
        gc.collect()
    
    def _make_layer(self):
        ## first Shard
        if self.is_first:
            self.embeddings = ViTEmbeddings(self.config)
            print(f">>>> Load embeddings layer for the first shard ")
            if self.load_weight:
                self.load_layer_weights(0, None, load_first = True, load_last=False, load_kernel = False, kernel_id=None)
                print(f">>>> Load weights for embeddings layer ")

        ## first ununit part 
        if self.start_layer %4 != 1 or (self.start_layer+3 > self.end_layer):
            self.has_first_ununit = True
            print(">>>> For the first model part, load weight is {self.load_weight}:")
            for i in range(self.start_layer, min(self.end_layer, math.ceil(self.start_layer/4)*4)+1):
                print(f"    Load the {i%4}-th operation ({operators_list[(i-1)%4]}) for {math.ceil(i/4)-1}-th vit layer")
                layer = self._build_kernel(i%4, math.ceil(i/4)-1, self.load_weight)
                if self.load_weight:
                    layer = self.load_layer_weights(math.ceil(i/4)-1, layer, False, False, True, i%4)
                self.first_ops.append(layer)
                del layer
                gc.collect()
            self.current_layer_idx = min(self.end_layer+1, math.ceil(self.start_layer/4)*4+1)

        ## mid unit part, the whole vit_layer
        while self.current_layer_idx + 3 <= self.end_layer:
            self.has_mid_unit = True
            layer = ViTLayer(self.config)
            if self.load_weight:
                layer = self.load_layer_weights(math.ceil(self.current_layer_idx/4)-1, layer)
            self.vit_layers.append(layer)
            del layer
            gc.collect()
            print(f">>>> Load the {math.ceil(self.current_layer_idx/4)-1}-th ViT Layer, load weight is {self.load_weight}")
            self.current_layer_idx += 4
        
        ## last unit part
        if self.end_layer >= self.current_layer_idx:
            print(">>>> For the last model part, load weight is {self.load_weight}:")
        for i in range(self.current_layer_idx, self.end_layer+1):
            self.has_last_ununit = True
            print(f"    Load the {i%4}-th operation ({operators_list[(i-1)%4]}) for {math.ceil(i/4)-1}-th vit layer")
            layer = self._build_kernel(i%4, math.ceil(i/4)-1, self.load_weight)
            if self.load_weight:
                layer = self.load_layer_weights(math.ceil(i/4)-1, layer, False, False, True, i%4)
            self.last_ops.append(layer)
            del layer
            gc.collect()
        
        ## last Shard
        if self.is_last:
            num_label = self.config.num_labels
            self.layernorm = nn.LayerNorm(self.config.hidden_size, eps=self.config.layer_norm_eps)
            print(f">>>> Load layernorm for the last shard")
            if self.model_name == 'google/vit-huge-patch14-224-in21k':
                num_label = 21843
            self.classifier = nn.Linear(self.config.hidden_size, num_label) if self.config.num_labels > 0 else nn.Identity()
            print(f">>>> Load classifier for the last shard")
            if self.load_weight:
                self.load_layer_weights(0, None, load_first = False, load_last=True, load_kernel = False, kernel_id=None)
                print(">>>> Load weights for layernorm and last shard")


        if self.load_weight:
            print(">>>> Finish load weights")
        else:
            print(">>>> Do NOT load weights")

    def _build_kernel(self, kernel_id, vit_layer_id, load_weight=True):
        layers = nn.ModuleList()
        if kernel_id == 1:
            layers.append(nn.LayerNorm(self.config.hidden_size, eps=self.config.layer_norm_eps))
            layers.append(ViTSelfAttention(self.config))
        elif kernel_id == 2:
            layers.append(ViTSelfOutput(self.config))
        elif kernel_id == 3:
            layers.append(nn.LayerNorm(self.config.hidden_size, eps=self.config.layer_norm_eps))
            layers.append( ViTIntermediate(self.config))
        else:
            layers.append(ViTOutput(self.config))
        if load_weight:
            self.load_layer_weights(vit_layer_id, layers, False, False, load_weight, kernel_id)
        return layers

    def load_layer_weights(self, id, transformer_layer, load_first = False, load_last=False, load_kernel = False, kernel_id=None):
        weights = np.load(self.weights_file_name)
        print(f" Hereeeeeeee 1\n\n memory {process.memory_info().rss // 1000000} MB")
        ROOT = f"Transformer/encoderblock_{id}"
        ATTENTION_Q = "MultiHeadDotProductAttention_1/query"
        ATTENTION_K = "MultiHeadDotProductAttention_1/key"
        ATTENTION_V = "MultiHeadDotProductAttention_1/value"
        ATTENTION_OUT = "MultiHeadDotProductAttention_1/out"
        FC_0 = "MlpBlock_3/Dense_0"
        FC_1 = "MlpBlock_3/Dense_1"
        ATTENTION_NORM = "LayerNorm_0"
        MLP_NORM = "LayerNorm_2"
        self.hidden_size = self.config.hidden_size
        if load_first:
            with torch.no_grad():
                self.embeddings.position_embeddings.copy_(torch.from_numpy((weights["Transformer/posembed_input/pos_embedding"])))
                conv_weight = weights["embedding/kernel"]
                O, I, J, K = conv_weight.shape
                # print(f"conv_shape is {O, I, J, K}, pe weight shape is {self.embeddings.patch_embeddings.projection.weight.shape}")
                # conv_weight = conv_weight.reshape(K,J,O,I)
                conv_weight = conv_weight.transpose([3, 2, 0, 1])
                self.embeddings.patch_embeddings.projection.weight.copy_(torch.from_numpy(conv_weight))
                self.embeddings.patch_embeddings.projection.bias.copy_(torch.from_numpy(weights["embedding/bias"]))
                # print(f">>>> Load embedding for the first shard")

        if load_last:
            with torch.no_grad():
                self.layernorm.weight.copy_(torch.from_numpy(weights["Transformer/encoder_norm/scale"]))
                self.layernorm.bias.copy_(torch.from_numpy(weights["Transformer/encoder_norm/bias"]))
                head_kernel = np.transpose(weights["head/kernel"])  
                # print(f"classifier weight is {self.classifier.weight.shape}, head kernel weight shape is {head_kernel.shape}")
                self.classifier.weight.copy_(torch.from_numpy(head_kernel))
                self.classifier.bias.copy_(torch.from_numpy(weights["head/bias"]))  
                # print(f">>>> Load Layernorm, classifier for the last shard")  

        if load_first == False and load_last == False:
            with torch.no_grad():
                if load_kernel == False:
                
                    query_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_Q, "kernel")]).view(self.hidden_size, self.hidden_size).t()
                    key_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_K, "kernel")]).view(self.hidden_size, self.hidden_size).t()
                    value_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_V, "kernel")]).view(self.hidden_size, self.hidden_size).t()
                    out_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_OUT, "kernel")]).view(self.hidden_size, self.hidden_size).t()

                    query_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_Q, "bias")]).view(-1)
                    key_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_K, "bias")]).view(-1)
                    value_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_V, "bias")]).view(-1)
                    out_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_OUT, "bias")]).view(-1)

                    transformer_layer.attention.attention.query.weight.copy_(query_weight)
                    transformer_layer.attention.attention.key.weight.copy_(key_weight)
                    transformer_layer.attention.attention.value.weight.copy_(value_weight)
                    transformer_layer.attention.output.dense.weight.copy_(out_weight)

                    transformer_layer.attention.attention.query.bias.copy_(query_bias)
                    transformer_layer.attention.attention.key.bias.copy_(key_bias)
                    transformer_layer.attention.attention.value.bias.copy_(value_bias)
                    transformer_layer.attention.output.dense.bias.copy_(out_bias)

                    mlp_weight_0 = torch.from_numpy(weights[os.path.join(ROOT, FC_0, "kernel")]).t()
                    mlp_weight_1 = torch.from_numpy(weights[os.path.join(ROOT, FC_1, "kernel")]).t()
                    mlp_bias_0 = torch.from_numpy(weights[os.path.join(ROOT, FC_0, "bias")]).t()
                    mlp_bias_1 = torch.from_numpy(weights[os.path.join(ROOT, FC_1, "bias")]).t()

                    transformer_layer.intermediate.dense.weight.copy_(mlp_weight_0)
                    transformer_layer.intermediate.dense.bias.copy_(mlp_bias_0)
                    transformer_layer.output.dense.weight.copy_(mlp_weight_1)
                    transformer_layer.output.dense.bias.copy_(mlp_bias_1)

                    transformer_layer.layernorm_before.weight.copy_(torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_NORM, "scale")]))
                    transformer_layer.layernorm_before.bias.copy_(torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_NORM, "bias")]))
                    transformer_layer.layernorm_after.weight.copy_(torch.from_numpy(weights[os.path.join(ROOT, MLP_NORM, "scale")]))
                    transformer_layer.layernorm_after.bias.copy_(torch.from_numpy(weights[os.path.join(ROOT, MLP_NORM, "bias")]))
                    print(f" Hereeeeeeee 2\n\n memory {process.memory_info().rss // 1000000} MB")
                    del query_weight, key_weight, value_weight, query_bias, key_bias, value_bias, mlp_weight_0, mlp_weight_1,mlp_bias_0, mlp_bias_1
                elif kernel_id == 1:

                    query_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_Q, "kernel")]).view(self.hidden_size, self.hidden_size).t()
                    key_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_K, "kernel")]).view(self.hidden_size, self.hidden_size).t()
                    value_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_V, "kernel")]).view(self.hidden_size, self.hidden_size).t()

                    query_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_Q, "bias")]).view(-1)
                    key_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_K, "bias")]).view(-1)
                    value_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_V, "bias")]).view(-1)

                    transformer_layer[0].weight.copy_(torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_NORM, "scale")]))
                    transformer_layer[0].bias.copy_(torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_NORM, "bias")]))
                    transformer_layer[1].query.weight.copy_(query_weight)
                    transformer_layer[1].key.weight.copy_(key_weight)
                    transformer_layer[1].value.weight.copy_(value_weight)

                    transformer_layer[1].query.bias.copy_(query_bias)
                    transformer_layer[1].key.bias.copy_(key_bias)
                    transformer_layer[1].value.bias.copy_(value_bias)
                    del query_weight, key_weight, value_weight, query_bias, key_bias, value_bias
                elif kernel_id == 2:
                    out_weight = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_OUT, "kernel")]).view(self.hidden_size, self.hidden_size).t()
                    out_bias = torch.from_numpy(weights[os.path.join(ROOT, ATTENTION_OUT, "bias")]).view(-1)
                    transformer_layer[0].dense.weight.copy_(out_weight)
                    transformer_layer[0].dense.bias.copy_(out_bias)
                    del out_weight, out_bias
                elif kernel_id == 3:
                    transformer_layer[0].weight.copy_(torch.from_numpy(weights[os.path.join(ROOT, MLP_NORM, "scale")]))
                    transformer_layer[0].bias.copy_(torch.from_numpy(weights[os.path.join(ROOT, MLP_NORM, "bias")]))
                    mlp_weight_0 = torch.from_numpy(weights[os.path.join(ROOT, FC_0, "kernel")]).t()
                    mlp_bias_0 = torch.from_numpy(weights[os.path.join(ROOT, FC_0, "bias")]).t()
                    transformer_layer[1].dense.weight.copy_(mlp_weight_0)
                    transformer_layer[1].dense.bias.copy_(mlp_bias_0)
                    del mlp_weight_0, mlp_bias_0
                elif kernel_id == 0:
                    mlp_weight_1 = torch.from_numpy(weights[os.path.join(ROOT, FC_1, "kernel")]).t()
                    mlp_bias_1 = torch.from_numpy(weights[os.path.join(ROOT, FC_1, "bias")]).t()
                    transformer_layer[0].dense.weight.copy_(mlp_weight_1)
                    transformer_layer[0].dense.bias.copy_(mlp_bias_1)
                    del mlp_weight_1, mlp_bias_1

        print(f" Hereeeeeeee 3\n\n memory {process.memory_info().rss // 1000000} MB")
        del weights
        gc.collect()
        return transformer_layer
    
    def forward_kernel(self, layer, x, skip, kernel_id):
        if kernel_id == 1:
            x = layer[0](x)
            x = layer[1](x)[0]
        elif kernel_id == 2:
            x = layer[0](x, skip)
            x += skip
            skip = x
        elif kernel_id == 3:
            x = layer[0](x)
            x = layer[1](x)
        else:
            x = layer[0](x, skip)
            skip = x
        return x, skip
    
    def _dequantize(self, x, scale, zero_point):
        xdq = (x - zero_point) * scale
        return xdq


    @torch.no_grad()
    def forward(self, x_rref):
        if self.is_first:
            x = x_rref.to_here()
        else:
            x, skip = x_rref.to_here()
            print(f"After receive, x is {x}, skip is {skip}, miaomiaomiao??? dequantizaiotn {x.dequantize()}")
            x = x.dequantize()
            skip = skip.dequantize()
            print(f"After dequantize, x is {x}, skip is {skip}")
        del x_rref

        with self._lock:
            start = time.time()
            if self.is_first:
                x = self.embeddings(x)
                skip = x

            if self.has_first_ununit:
                for i in range(len(self.first_ops)):
                    x, skip = self.forward_kernel(self.first_ops[i], x, skip, (self.start_layer+i)%4)

            for i, layer in enumerate(self.vit_layers):
                x = layer(x)[0]
                skip = x

            if self.has_last_ununit:
                for i in range(len(self.last_ops)):
                    x, skip = self.forward_kernel(self.last_ops[i], x, skip, (self.current_layer_idx+i)%4)

            if self.is_last:
                x = self.layernorm(x)
                x = self.classifier(x[:, 0, :])
            end = time.time()

        self.total_time +=  (end - start)
        self.total_batch += 1
        print(f"Round {self.total_batch}: memory {process.memory_info().rss // 1000000} MB")
        print(f"Shard{self.rank} finishes {self.total_batch} microbatch, time is {end -start}, total time is {self.total_time}")
        if self.is_last:
            return x
        gc.collect()
        self.observer(x)
        self.x_scale, self.x_zp = self.observer.calculate_qparams()
        print(f"X scale is {self.x_scale}, zp is {self.x_zp}")
        print(f"max x is {torch.max(x)}, min is {torch.min(x)}")
        self.observer(skip)
        self.s_scale, self.s_zp = self.observer.calculate_qparams()
        print(f"Before quantization, x is {x}, skip is {skip}")
        xq = torch.quantize_per_tensor(x, scale = float(self.x_scale), zero_point = int(self.x_zp), dtype=torch.qint8)
        sq = torch.quantize_per_tensor(skip, scale = float(self.s_scale), zero_point = int(self.s_zp), dtype=torch.qint8)
        print(f"After quantization, xq is {xq}, int is {xq.int_repr()}, skip is {sq}, int is {sq.int_repr()}")
        print(f"have a try dequantizaition here {xq.dequantize()}")
        return xq, sq

    def parameter_rrefs(self):
        return [RRef(p) for p in self.parameters()]


#########################################################
#                Build Transformer Shard                #
#########################################################

## Class factory
def _create_transformershard(class_name, rank, model_name, is_first, is_last, start_layer, end_layer, load_weight=True):
    class TransformerShardCls(TransformerBase):
        def __init__(self):
            super(TransformerShardCls, self).__init__(rank, model_name, is_first, is_last, start_layer, end_layer, load_weight=True)
    TransformerShardCls.__qualname__ = class_name
    return TransformerShardCls


_shard_class = [f'TransformerShard{i+1}' for i in range(total_rank)]

rank = 0
shard_class_list = []
for _name in _shard_class:
    if rank == 0:
        globals()[_name] = _create_transformershard(_name, rank, model_name, True, False, partition[2*rank], partition[2*rank+1], True )
    elif rank == total_rank-1:
        globals()[_name] = _create_transformershard(_name, rank, model_name, False, True, partition[2*rank], partition[2*rank+1], True )
    else:
        globals()[_name] = _create_transformershard(_name, rank, model_name, False, False, partition[2*rank], partition[2*rank+1], True )
    shard_class_list.append(eval(_name))
    rank += 1


#########################################################
#             Stitch Shards into one Module             #
#########################################################
class DistTransformer(nn.Module):
    def __init__(self, model_name, num_split, workers, *args, **kwargs):
        super(DistTransformer, self).__init__()
        self.num_split = num_split
        self.rref_list = []
        for i in range(total_rank):
            exec(f"self.p{i+1}_rref= rpc.remote(workers[{i}],shard_class_list[{i}])")
            self.rref_list.append(eval(f"self.p{i+1}_rref"))

    def forward(self, xs):
        out_futures = []
        for x in iter(xs.split(self.num_split, dim=0)):
            x_rref = RRef(x)
            for i in range(total_rank):
                if i == 0:
                    y_rref = self.rref_list[i].remote().forward(x_rref)
                elif i == total_rank-1:
                    z_rref = self.rref_list[i].rpc_async().forward(y_rref)
                     
                else:
                    y_rref = self.rref_list[i].remote().forward(y_rref)
                # rref_info = _rref_context_get_debug_info()
                # debug_info = _get_debug_info()
                # print(f"rref info {rref_info}")
                # print(f"debug info {debug_info}")
            
            # y_rref.to_here()
            x_rref.to_here()
            del y_rref
            del x_rref
            out_futures.append(z_rref)
            del z_rref
            gc.collect()
        return torch.cat(torch.futures.wait_all(out_futures))



#########################################################
#                   Run RPC Processes                   #
#########################################################

latencies = []
throughputs = []

feature_extractor = ViTFeatureExtractor.from_pretrained(model_name)

def EuclideanDistances(a,b):
    sq_a = a**2
    sum_sq_a = torch.sum(sq_a,dim=1).unsqueeze(1)  # m->[m, 1]
    sq_b = b**2
    sum_sq_b = torch.sum(sq_b,dim=1).unsqueeze(0)  # n->[1, n]
    bt = b.t()
    return torch.sqrt(sum_sq_a+sum_sq_b-2*a.mm(bt))

def run_master(split_size):
    # put the two model parts on worker1 and worker2 respectively
    print("Run mastering \n")
    work_list = [f"worker{i}" for i in range(total_rank)]
    ## for verification 
    origin_model = ViTForImageClassification.from_pretrained(model_name)
    for si in range(len(split_size)):
        # print(f"Start Calcluate split size {split_size[si]}")
        model =  DistTransformer(model_name, split_size[si], work_list)
        inputs = feature_extractor(images=imgs, return_tensors="pt")
        tik = time.time()
        for i in range(num_batches):
            # generate random inputs and labels       
            outputs = model(inputs['pixel_values'])
            # del outputs
            # gc.collect()
            predicted_class_idx = outputs[0].argmax(-1).item()
            true_output = origin_model(**inputs)
            logits = true_output.logits
            print(f"logits.shape, outputs.shape, the distance is {torch.dist(outputs, logits)}")
            print("Predicted class:", origin_model.config.id2label[predicted_class_idx])
        ## Calculate time
        tok = time.time()
        latency = tok-tik
        throughput = num_batches*batch_size / latency
        # print(f"Split size is {split_size[si]}, Total program execution time = {tok - tik}")
        latencies.append(latency)
        throughputs.append(throughput)
         
    best_choice = -1
    best_throughput  = -1
    for i in range(len(split_size)):
        print(f"Split size {split_size[i]}, latency is {latencies[i]}, throughput is {throughputs[i]}")
        if throughputs[i] > best_throughput:
            best_throughput = throughputs[i]
            best_choice = i 
    print("\n---------------- Split output line ----------------")
    print(f"\nBest split size is {split_size[best_choice]}, Execution time is {latencies[best_choice]}, throughput is {throughputs[best_choice]}\n")
    
   


def run_worker(rank, world_size, num_split):

    os.environ['MASTER_ADDR'] = MASTER_ADDR #'10.52.3.175' #'127.0.0.1' # '172.30.0.21'
    os.environ['MASTER_PORT'] = MASTER_PORT
    os.environ["TP_SOCKET_IFNAME"] = SOCKET_IFNAME
    os.environ["GLOO_SOCKET_IFNAME"] = SOCKET_IFNAME

    # Higher timeout is added to accommodate for kernel compilation time in case of ROCm.
    options = rpc.TensorPipeRpcBackendOptions(num_worker_threads=num_worker_threads,rpc_timeout=3000)


    if rank == 0:
        rpc.init_rpc(
            "worker0",
            rank=rank,
   #         backend=rpc.BackendType.PROCESS_GROUP,
            world_size=world_size,
            rpc_backend_options=options
        )
        run_master(num_split)
    else:
        rpc.init_rpc(
            f"worker{rank}",
            rank=rank,
   #         backend=rpc.BackendType.PROCESS_GROUP,
            world_size=world_size,
            rpc_backend_options=options
        )
        pass

    # block until all rpcs finisha
    rpc.shutdown()

if __name__=="__main__":
    world_size = total_rank
    rank=int(sys.argv[1])
    num_split= splits

    print(f"Model name is {model_name}, Batch size is {batch_size}, Split size is: {num_split}, \n Split method is {partition}, GLOO Threads is {num_worker_threads}")
    
    tik = time.time()
    run_worker(rank, world_size, num_split)
    tok = time.time()
    print(f"Total program execution time = {tok - tik}")