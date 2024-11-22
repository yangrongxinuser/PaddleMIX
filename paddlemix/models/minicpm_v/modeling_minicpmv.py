import paddle
import math
from typing import List
import json
from threading import Thread
from copy import deepcopy
from PIL import Image
from .configuration_minicpm import MiniCPMVConfig
from .modeling_navit_siglip import SigLipVisionTransformer
from .resampler import Resampler
from paddlenlp.transformers import Qwen2PretrainedModel,Qwen2ForCausalLM
import numpy as np
from paddlenlp.generation import TextIteratorStreamer
from paddlemix.processors.processing_minicpmv import MiniCPMVProcessor
from paddlemix.processors.image_processing_minicpmv import  MiniCPMVImageProcessor
import paddle
import numpy as np
from datetime import datetime
import sys

def analyze_paddle_weights(model, output_file='paddle_weights_analysis.txt'):
    """
    分析并保存 Paddle 模型的权重信息到文本文件，支持 bfloat16
    
    Args:
        model: paddle.nn.Layer 模型实例
        output_file: str 输出文件路径
    """
    def _print_layer_weights(model, prefix='', file=sys.stdout):
        for name, layer in model.named_children():
            current_prefix = f"{prefix}.{name}" if prefix else name
            
            # 处理Linear层
            if isinstance(layer, paddle.nn.Linear):
                weight = layer.weight
                bias = layer.bias
                print(f"\n{current_prefix}:", file=file)
                print(f"  Weight shape: {weight.shape}", file=file)
                print(f"  Weight dtype: {weight.dtype}", file=file)
                
                # 转换权重数据类型
                weight_data = weight.astype('float32').numpy()
                print(f"  Weight statistics:", file=file)
                print(f"    Mean: {weight_data.mean():.6f}", file=file)
                print(f"    Std: {weight_data.std():.6f}", file=file)
                print(f"    Min: {weight_data.min():.6f}", file=file)
                print(f"    Max: {weight_data.max():.6f}", file=file)
                
                if bias is not None:
                    print(f"  Bias shape: {bias.shape}", file=file)
                    print(f"  Bias dtype: {bias.dtype}", file=file)
                    bias_data = bias.astype('float32').numpy()
                    print(f"  Bias statistics:", file=file)
                    print(f"    Mean: {bias_data.mean():.6f}", file=file)
                    print(f"    Std: {bias_data.std():.6f}", file=file)
                    print(f"    Min: {bias_data.min():.6f}", file=file)
                    print(f"    Max: {bias_data.max():.6f}", file=file)
            
            # 处理Embedding层
            elif isinstance(layer, paddle.nn.Embedding):
                weight = layer.weight
                print(f"\n{current_prefix}:", file=file)
                print(f"  Embedding weight shape: {weight.shape}", file=file)
                print(f"  Weight dtype: {weight.dtype}", file=file)
                
                weight_data = weight.astype('float32').numpy()
                print(f"  Weight statistics:", file=file)
                print(f"    Mean: {weight_data.mean():.6f}", file=file)
                print(f"    Std: {weight_data.std():.6f}", file=file)
                print(f"    Min: {weight_data.min():.6f}", file=file)
                print(f"    Max: {weight_data.max():.6f}", file=file)
            
            # 递归处理子层
            elif hasattr(layer, 'named_children'):
                _print_layer_weights(layer, current_prefix, file)

    with open(output_file, 'w', encoding='utf-8') as f:
        # 写入分析时间和基本信息
        print(f"Model Weights Analysis Report", file=f)
        print(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", file=f)
        print(f"Model type: {type(model).__name__}", file=f)
        print("-" * 80, file=f)
        
        try:
            # 写入详细的层信息
            _print_layer_weights(model, file=f)
            print("\nAnalysis completed successfully!", file=f)
        except Exception as e:
            print(f"\nError occurred during analysis: {str(e)}", file=f)
            raise

        # 打印总结信息
        print("\nSummary:", file=f)
        total_params = sum(p.size for p in model.parameters())
        print(f"Total parameters: {total_params:,}", file=f)
        print(f"Total layers analyzed: {len(list(model.named_children()))}", file=f)

# 使用方法：
# model = your_paddle_model
# analyze_paddle_weights(model, 'paddle_model_analysis.txt')

class MiniCPMVPreTrainedModel(Qwen2PretrainedModel):
    config_class = MiniCPMVConfig

def pad_sequence(sequences, padding_value=0, fix_len=None):
    """Fill sequences(np.ndarray) into a fixed-length matrix."""
    max_size = sequences[0].shape
    trailing_dims = tuple(max_size[1:])
    max_len = max([s.shape[0] for s in sequences])
    if fix_len is not None:
        assert fix_len >= max_len, "fix_len is too small."
        max_len = fix_len
    out_dims = (len(sequences), max_len) + trailing_dims
    
    # Convert Paddle dtype to numpy dtype
    dtype = np.float32 if sequences[0].dtype == paddle.float32 else sequences[0].numpy().dtype
    
    out_tensor = np.full(out_dims, padding_value, dtype=dtype)
    for i, tensor in enumerate(sequences):
        length = tensor.shape[0]
        out_tensor[i, :length, ...] = tensor
    return out_tensor

class MiniCPMV(MiniCPMVPreTrainedModel):

    def __init__(self, config):
        super().__init__(config)
        self.llm = Qwen2ForCausalLM(config)
        self.vpm = self.init_vision_module()
        self.vision_dim = self.vpm.embed_dim
        self.embed_dim = self.llm.config.hidden_size
        self.resampler = self.init_resampler(self.embed_dim, self.vision_dim)
        self.processor = None
        self.terminators = ['<|im_end|>', '<|endoftext|>']

    def init_vision_module(self):
        # TODO:
        # if self.config._attn_implementation == 'flash_attention_2':
        #     self.config.vision_config._attn_implementation = (
        #         'flash_attention_2')
        # else:
        self.config.vision_config._attn_implementation = 'eager'
        model = SigLipVisionTransformer(self.config.vision_config)
        if self.config.drop_vision_last_layer:
            model.encoder.layers = model.encoder.layers[:-1]
        setattr(model, 'embed_dim', model.embeddings.embed_dim)
        setattr(model, 'patch_size', model.embeddings.patch_size)
        return model

    def init_resampler(self, embed_dim, vision_dim):
        return Resampler(num_queries=self.config.query_num, embed_dim=
            embed_dim, num_heads=embed_dim // 128, kv_dim=vision_dim,
            adaptive=True)

    def get_input_embeddings(self):
        return self.llm.get_input_embeddings()

    def set_input_embeddings(self, value):
        self.llm.embed_tokens = value

    def get_output_embeddings(self):
        return self.llm.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.llm.lm_head = new_embeddings

    def set_decoder(self, decoder):
        self.llm = decoder

    def get_decoder(self):
        return self.llm

    def get_vllm_embedding(self, data):
        if 'vision_hidden_states' not in data:
            dtype = self.llm.qwen2.embed_tokens.weight.dtype
            # device = self.llm.qwen2.embed_tokens.weight.place
            tgt_sizes = data['tgt_sizes']
            pixel_values_list = data['pixel_values']
            vision_hidden_states = []
            all_pixel_values = []
            img_cnt = []
            for pixel_values in pixel_values_list:
                img_cnt.append(len(pixel_values))
                all_pixel_values.extend([i.flatten(stop_axis=1).transpose([1, 0]) for i in pixel_values])

            # exist image
            if all_pixel_values:
                tgt_sizes = [tgt_size for tgt_size in tgt_sizes if isinstance(tgt_size, paddle.Tensor)]
                tgt_sizes = paddle.stack(tgt_sizes).squeeze(0).astype('int32')

                if self.config.batch_vision_input:
                    max_patches = paddle.max(tgt_sizes[:, 0] * tgt_sizes[:, 1])

                    all_pixel_values = pad_sequence(all_pixel_values, padding_value=0.0)
                    B, L, _ = all_pixel_values.shape
                    all_pixel_values = all_pixel_values.transpose([0, 2, 1]).reshape([B, 3, -1, L])

                    
                    patch_attn_mask = paddle.zeros([B, 1, max_patches], dtype='bool')
                    for i in range(B):
                        patch_attn_mask[i, 0, :tgt_sizes[i][0] * tgt_sizes[
                            i][1]] = True
                    # import pdb; pdb.set_trace()
                    vision_embedding = self.vpm(paddle.to_tensor(all_pixel_values).cast(dtype), patch_attention_mask=patch_attn_mask, tgt_sizes=tgt_sizes).last_hidden_state
                    vision_embedding = self.resampler(vision_embedding, tgt_sizes)
                else:
                    # get vision_embedding foreach
                    vision_embedding = []
                    for single_tgt_size, single_pixel_values in zip(tgt_sizes, all_pixel_values):
                        single_pixel_values = single_pixel_values.unsqueeze(0)
                        B, L, _ = single_pixel_values.shape
                        single_pixel_values = single_pixel_values.transpose([0, 2, 1]).reshape([B, 3, -1, L])
                        single_vision_embedding = self.vpm(single_pixel_values.astype(dtype), tgt_sizes=single_tgt_size.unsqueeze(0)).last_hidden_state
                        single_vision_embedding = self.resampler(single_vision_embedding, single_tgt_size.unsqueeze(0))
                        vision_embedding.append(single_vision_embedding)
                    vision_embedding = paddle.concat(vision_embedding, axis=0)

                start = 0
                for pixel_values in pixel_values_list:
                    img_cnt = len(pixel_values)
                    if img_cnt > 0:
                        vision_hidden_states.append(vision_embedding[start: start + img_cnt])
                        start += img_cnt
                    else:
                        vision_hidden_states.append([])
            else: # no image
                if self.training:
                    dummy_image = paddle.zeros(
                        [1, 3, 224, 224],
                        dtype=dtype
                    )
                    tgt_sizes = paddle.to_tensor([[[224 // self.config.patch_size, math.ceil(224 / self.config.patch_size)]]], dtype='int32')
                    dummy_feature = self.resampler(self.vpm(dummy_image).last_hidden_state, tgt_sizes)
                else:
                    dummy_feature = []
                for _ in range(len(pixel_values_list)):
                    vision_hidden_states.append(dummy_feature)

        else:
            vision_hidden_states = data['vision_hidden_states']

        if hasattr(self.llm.config, 'scale_emb'):
            vllm_embedding = self.llm.qwen2.embed_tokens(data['input_ids']) * self.llm.config.scale_emb
        else:
            vllm_embedding = self.llm.qwen2.embed_tokens(data['input_ids'])

        vision_hidden_states = [i.astype(vllm_embedding.dtype) if isinstance(
            i, paddle.Tensor) else i for i in vision_hidden_states]
        bs = len(data['input_ids'])
        for i in range(bs):
            cur_vs_hs = vision_hidden_states[i]
            if len(cur_vs_hs) > 0:
                cur_vllm_emb = vllm_embedding[i]
                cur_image_bound = data['image_bound'][i]
                if len(cur_image_bound) > 0:
                    image_indices = paddle.stack(x=[paddle.arange(start=r[0
                        ], end=r[1], dtype='int64') for r in cur_image_bound]
                        ).to(vllm_embedding.place)

                    cur_vllm_emb.put_along_axis_(axis=0, indices=
                        image_indices.reshape([-1, 1]).tile(repeat_times=[1,
                        tuple(cur_vllm_emb.shape)[-1]]), values=cur_vs_hs.
                        reshape([-1, tuple(cur_vs_hs.shape)[-1]]))
                elif self.training:
                    cur_vllm_emb += cur_vs_hs[0].mean() * 0
        return vllm_embedding, vision_hidden_states

    def forward(self, data, **kwargs):
        vllm_embedding, vision_hidden_states = self.get_vllm_embedding(data)
        position_ids = data['position_ids']
        if position_ids.dtype != 'int64':
            position_ids = position_ids.astype(dtype='int64')
        return self.llm(
            input_ids=None,
            position_ids=position_ids,
            inputs_embeds=vllm_embedding,
            **kwargs,
        )

    def _decode(self, inputs_embeds, tokenizer, attention_mask, decode_text=False, **kwargs):
        terminators = [tokenizer.convert_tokens_to_ids(i) for i in self.terminators]

        ###  must add position_ids, paddlenlp bug
        batch_size, seq_length = attention_mask.shape
        position_ids = paddle.arange(seq_length).expand((batch_size, seq_length))
        ###

        output = self.llm.generate(
            position_ids=position_ids, ####
            inputs_embeds=inputs_embeds, # [1, 359, 3584] sum -7040  mean -0.00546265
            pad_token_id=0,
            eos_token_id=terminators, # [151645, 151643]
            attention_mask=attention_mask, # [1, 359]
            **kwargs, # {'max_new_tokens': 2048, 'top_p': 0.01, 'top_k': 100, 'temperature': 0.7, 'do_sample': True, 'repetition_penalty': 1.05}
        )[0]
        #print('output:\n', output)
        #import pdb; pdb.set_trace()
        
        # output = paddle.to_tensor([[151643,    785,   2168,  61891,    264,   2518,  88222,     11,    892,
        #     374,   9867,    311,    279,  23149,  75338,  97340,    323,  98811,
        #    5616,     13,   3731,  18617,    525,   3881,    369,    862,  62144,
        #     812,   1455,   4830,  18241,     11,   4158,  27800,  64072,     11,
        #     323,  29673,     88,   9787,     13,   2379,    525,  58488,  89768,
        #    9898,     11,  10164,   1429,    315,    862,    882,    304,  12408,
        #      11,    892,    374,  25911,    553,    279,  22360,   5944,    807,
        #     525,  44730,    448,    304,    419,   6548,     13,    576,   6243,
        #    7952,    311,    387,    264,  14071,   4573,     11,  10767,    264,
        #   40914,    476,  29305,  50539,     11,   2661,    279,   9362,    315,
        #     883,  26877,  14389,   6188,    311,  55359,   5810,  70599,     13,
        #     576,   2518,  88222,    594,   7493,    374,    825,    315,  40228,
        #     476,  23034,   2734,     11,    438,    432,   5868,   5961,    518,
        #     279,   6249,     13,   1096,   9419,    374,   3545,   8604,    304,
        #   82919,   4152,    311,   1181,  28611,   2639,     11,   1660,  10007,
        #     438,  19563,    389,    279,    358,   5459,     45,   3731,   1759,
        #      13, 151645]], dtype=paddle.int64)
        
        if decode_text:
            return self._decode_text(output, tokenizer)
        return output

    def _decode_stream(self, inputs_embeds, tokenizer, **kwargs):
        terminators = [tokenizer.convert_tokens_to_ids(i) for i in self.
            terminators]
        streamer = TextIteratorStreamer(tokenizer=tokenizer)
        generation_kwargs = {'inputs_embeds': inputs_embeds, 'pad_token_id':
            0, 'eos_token_id': terminators, 'streamer': streamer}
        generation_kwargs.update(kwargs)
        thread = Thread(target=self.llm.generate, kwargs=generation_kwargs)
        """Class Method: *.start, can not convert, please check whether it is torch.Tensor.*/Optimizer.*/nn.Module.*/torch.distributions.Distribution.*/torch.autograd.function.FunctionCtx.*/torch.profiler.profile.*/torch.autograd.profiler.profile.*, and convert manually"""
        thread.start()
        return streamer

    def _decode_text(self, result_ids, tokenizer):
        terminators = [tokenizer.convert_tokens_to_ids(i) for i in self.terminators]
        result_text = []
        for result in result_ids:
            if result[0] == tokenizer.bos_id: # 151644
                result = result[1:]
            if result[-1] in terminators: # [151645, 151643]
                result = result[:-1]
            result_text.append(tokenizer.decode(result).strip())
        return result_text

    def generate(self, input_ids=None, pixel_values=None, tgt_sizes=None,
        image_bound=None, attention_mask=None, tokenizer=None,
        vision_hidden_states=None, return_vision_hidden_states=False,
        stream=False, decode_text=False, **kwargs):
        assert input_ids is not None
        assert len(input_ids) == len(pixel_values)
        model_inputs = {'input_ids': input_ids, 'image_bound': image_bound}
        if vision_hidden_states is None:
            model_inputs['pixel_values'] = pixel_values
            model_inputs['tgt_sizes'] = tgt_sizes
        else:
            model_inputs['vision_hidden_states'] = vision_hidden_states
        with paddle.no_grad():
            model_inputs['inputs_embeds'], vision_hidden_states = self.get_vllm_embedding(model_inputs)
            if stream:
                result = self._decode_stream(model_inputs['inputs_embeds'],
                    tokenizer, **kwargs)
            else:
                result = self._decode(model_inputs['inputs_embeds'],
                    tokenizer, attention_mask, decode_text=decode_text, **kwargs)
  
        if return_vision_hidden_states:
            return result, vision_hidden_states
        return result

    def chat(self, image, msgs, tokenizer, processor=None,
        vision_hidden_states=None, max_new_tokens=2048, min_new_tokens=0,
        sampling=True, max_inp_length=8192, system_prompt='', stream=False,
        max_slice_nums=None, use_image_id=None, **kwargs):
        if isinstance(msgs[0], list):
            batched = True
        else:
            batched = False
        msgs_list = msgs
        images_list = image
        if batched is False:
            images_list, msgs_list = [images_list], [msgs_list]
        assert len(images_list) == len(msgs_list
            ), 'The batch dim of images_list and msgs_list should be the same.'
        if processor is None:
            if self.processor is None:
                image_processor =  MiniCPMVImageProcessor.from_pretrained(self.config._name_or_path, trust_remote_code=True)
                self.processor = MiniCPMVProcessor(image_processor, tokenizer)
            processor = self.processor
        assert self.config.query_num == processor.image_processor.image_feature_size, 'These two values should be the same. Check `config.json` and `preprocessor_config.json`.'
        assert self.config.patch_size == processor.image_processor.patch_size, 'These two values should be the same. Check `config.json` and `preprocessor_config.json`.'
        assert self.config.use_image_id == processor.image_processor.use_image_id, 'These two values should be the same. Check `config.json` and `preprocessor_config.json`.'
        assert self.config.slice_config.max_slice_nums == processor.image_processor.max_slice_nums, 'These two values should be the same. Check `config.json` and `preprocessor_config.json`.'
        assert self.config.slice_mode == processor.image_processor.slice_mode, 'These two values should be the same. Check `config.json` and `preprocessor_config.json`.'
        prompts_lists = []
        input_images_lists = []
        for image, msgs in zip(images_list, msgs_list):
            if isinstance(msgs, str):
                msgs = json.loads(msgs)
            copy_msgs = deepcopy(msgs)
            assert len(msgs) > 0, 'msgs is empty'
            assert sampling or not stream, 'if use stream mode, make sure sampling=True'
            if image is not None and isinstance(copy_msgs[0]['content'], str):
                copy_msgs[0]['content'] = [image, copy_msgs[0]['content']]
            images = []
            for i, msg in enumerate(copy_msgs):
                role = msg['role']
                content = msg['content']
                assert role in ['user', 'assistant']
                if i == 0:
                    assert role == 'user', 'The role of first msg should be user'
                if isinstance(content, str):
                    content = [content]
                cur_msgs = []
                for c in content:
                    if isinstance(c, Image.Image):
                        images.append(c)
                        cur_msgs.append('(<image>./</image>)')
                    elif isinstance(c, str):
                        cur_msgs.append(c)
                msg['content'] = '\n'.join(cur_msgs)
            if system_prompt:
                sys_msg = {'role': 'system', 'content': system_prompt}
                copy_msgs = [sys_msg] + copy_msgs
            prompts_lists.append(
                processor.tokenizer.apply_chat_template(
                    copy_msgs, tokenize=False, add_generation_prompt=True))
            input_images_lists.append(images)
        
        # print('prompts_lists:\n', prompts_lists)
        inputs = processor(
            prompts_lists,
            input_images_lists,
            max_slice_nums=max_slice_nums,
            use_image_id=use_image_id,
            return_tensors='pd',
            max_length=max_inp_length,
        )

        if sampling:
            generation_config = {'top_p': 0.01, 'top_k': 100, 'temperature':0.7, 'do_sample': True, 'repetition_penalty': 1.05}
        else:
            generation_config = {'num_beams': 3, 'repetition_penalty': 1.2}
        if min_new_tokens > 0:
            generation_config['min_new_tokens'] = min_new_tokens
        generation_config.update((k, kwargs[k]) for k in generation_config.keys() & kwargs.keys())
        inputs.pop('image_sizes')

        with paddle.no_grad():
            res = self.generate(
                **inputs,
                tokenizer=tokenizer,
                max_new_tokens=max_new_tokens,
                vision_hidden_states=vision_hidden_states,
                stream=stream,
                decode_text=True,
                **generation_config,
            )

        if stream:
            def stream_gen():
                for text in res:
                    for term in self.terminators:
                        text = text.replace(term, '')
                    yield text
            return stream_gen()
        else:
            if batched:
                answer = res
            else:
                answer = res[0]
            return answer
