import base64
import gc
import json
import os
import tempfile
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import requests
import streamlit as st
import torch
from diffusers import (
    AltDiffusionImg2ImgPipeline,
    AltDiffusionPipeline,
    DiffusionPipeline,
    StableDiffusionImg2ImgPipeline,
    StableDiffusionPipeline,
)
from loguru import logger
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from st_clickable_images import clickable_images

from diffuzers import utils


def load_embed(learned_embeds_path, text_encoder, tokenizer, token=None):
    loaded_learned_embeds = torch.load(learned_embeds_path, map_location="cpu")
    if len(loaded_learned_embeds) > 2:
        embeds = loaded_learned_embeds["string_to_param"]["*"][-1, :]
    else:
        # separate token and the embeds
        trained_token = list(loaded_learned_embeds.keys())[0]
        embeds = loaded_learned_embeds[trained_token]

    # add the token in tokenizer
    token = token if token is not None else trained_token
    num_added_tokens = tokenizer.add_tokens(token)
    i = 1
    while num_added_tokens == 0:
        logger.warning(f"The tokenizer already contains the token {token}.")
        token = f"{token[:-1]}-{i}>"
        logger.info(f"Attempting to add the token {token}.")
        num_added_tokens = tokenizer.add_tokens(token)
        i += 1

    # resize the token embeddings
    text_encoder.resize_token_embeddings(len(tokenizer))

    # get the id for the token and assign the embeds
    token_id = tokenizer.convert_tokens_to_ids(token)
    text_encoder.get_input_embeddings().weight.data[token_id] = embeds
    return token


@dataclass
class X2Image:
    device: Optional[str] = None
    model: Optional[str] = None
    output_path: Optional[str] = None
    custom_pipeline: Optional[str] = None
    embeddings_url: Optional[str] = None
    token_identifier: Optional[str] = None

    def __str__(self) -> str:
        return f"X2Image(model={self.model}, pipeline={self.custom_pipeline})"

    def __post_init__(self):
        self.text2img_pipeline = DiffusionPipeline.from_pretrained(
            self.model,
            torch_dtype=torch.float16 if self.device == "cuda" else torch.float32,
            custom_pipeline=self.custom_pipeline,
            use_auth_token=utils.use_auth_token(),
        )
        components = self.text2img_pipeline.components

        if isinstance(self.text2img_pipeline, StableDiffusionPipeline):
            self.img2img_pipeline = StableDiffusionImg2ImgPipeline(**components)
        elif isinstance(self.text2img_pipeline, AltDiffusionPipeline):
            self.img2img_pipeline = AltDiffusionImg2ImgPipeline(**components)
        else:
            self.img2img_pipeline = None
            logger.error("Model type not supported, img2img pipeline not created")

        self.text2img_pipeline.to(self.device)
        self.text2img_pipeline.safety_checker = utils.no_safety_checker
        self.img2img_pipeline.to(self.device)
        self.img2img_pipeline.safety_checker = utils.no_safety_checker

        self.compatible_schedulers = {
            scheduler.__name__: scheduler for scheduler in self.text2img_pipeline.scheduler.compatibles
        }

        if len(self.embeddings_url) > 0 and len(self.token_identifier) > 0:
            # download the embeddings
            self.embeddings_path = utils.download_file(self.embeddings_url)
            load_embed(
                learned_embeds_path=self.embeddings_path,
                text_encoder=self.pipeline.text_encoder,
                tokenizer=self.pipeline.tokenizer,
                token=self.token_identifier,
            )

        if self.device == "mps":
            self.text2img_pipeline.enable_attention_slicing()
            prompt = "a photo of an astronaut riding a horse on mars"
            _ = self.text2img_pipeline(prompt, num_inference_steps=2)

            self.img2img_pipeline.enable_attention_slicing()
            url = "https://raw.githubusercontent.com/CompVis/stable-diffusion/main/assets/stable-samples/img2img/sketch-mountains-input.jpg"
            response = requests.get(url)
            init_image = Image.open(BytesIO(response.content)).convert("RGB")
            init_image.thumbnail((768, 768))
            prompt = "A fantasy landscape, trending on artstation"
            _ = self.img2img_pipeline(
                prompt=prompt,
                image=init_image,
                strength=0.75,
                guidance_scale=7.5,
                num_inference_steps=2,
            )

    def _set_scheduler(self, pipeline_name, scheduler_name):
        if pipeline_name == "text2img":
            scheduler_config = self.text2img_pipeline.scheduler.config
        elif pipeline_name == "img2img":
            scheduler_config = self.img2img_pipeline.scheduler.config
        else:
            raise ValueError(f"Pipeline {pipeline_name} not supported")

        scheduler = self.compatible_schedulers[scheduler_name].from_config(scheduler_config)

        if pipeline_name == "text2img":
            self.text2img_pipeline.scheduler = scheduler
        elif pipeline_name == "img2img":
            self.img2img_pipeline.scheduler = scheduler

    def _pregen(self, pipeline_name, scheduler, num_images, seed):
        self._set_scheduler(scheduler_name=scheduler, pipeline_name=pipeline_name)
        if self.device == "mps":
            generator = torch.manual_seed(seed)
            num_images = 1
        else:
            generator = torch.Generator(device=self.device).manual_seed(seed)
        num_images = int(num_images)
        return generator, num_images

    def _postgen(self, metadata, output_images, pipeline_name):
        torch.cuda.empty_cache()
        gc.collect()
        metadata = json.dumps(metadata)
        _metadata = PngInfo()
        _metadata.add_text(pipeline_name, metadata)
        utils.save_images(
            images=output_images,
            module=pipeline_name,
            metadata=metadata,
            output_path=self.output_path,
        )
        return output_images, _metadata

    def text2img_generate(
        self, prompt, negative_prompt, scheduler, image_size, num_images, guidance_scale, steps, seed
    ):
        generator, num_images = self._pregen(
            pipeline_name="text2img",
            scheduler=scheduler,
            num_images=num_images,
            seed=seed,
        )
        output_images = self.text2img_pipeline(
            prompt,
            negative_prompt=negative_prompt,
            width=image_size[1],
            height=image_size[0],
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            num_images_per_prompt=num_images,
            generator=generator,
        ).images
        metadata = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "scheduler": scheduler,
            "image_size": image_size,
            "num_images": num_images,
            "guidance_scale": guidance_scale,
            "steps": steps,
            "seed": seed,
        }

        output_images, _metadata = self._postgen(
            metadata=metadata,
            output_images=output_images,
            pipeline_name="text2img",
        )
        return output_images, _metadata

    def img2img_generate(
        self, prompt, image, strength, negative_prompt, scheduler, num_images, guidance_scale, steps, seed
    ):
        generator, num_images = self._pregen(
            pipeline_name="img2img",
            scheduler=scheduler,
            num_images=num_images,
            seed=seed,
        )
        output_images = self.img2img_pipeline(
            prompt=prompt,
            image=image,
            strength=strength,
            negative_prompt=negative_prompt,
            num_inference_steps=steps,
            guidance_scale=guidance_scale,
            num_images_per_prompt=num_images,
            generator=generator,
        ).images
        metadata = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "scheduler": scheduler,
            "num_images": num_images,
            "guidance_scale": guidance_scale,
            "steps": steps,
            "seed": seed,
        }
        output_images, _metadata = self._postgen(
            metadata=metadata,
            output_images=output_images,
            pipeline_name="img2img",
        )
        return output_images, _metadata

    def app(self):
        available_schedulers = list(self.compatible_schedulers.keys())
        if "EulerAncestralDiscreteScheduler" in available_schedulers:
            available_schedulers.insert(
                0, available_schedulers.pop(available_schedulers.index("EulerAncestralDiscreteScheduler"))
            )
        # col3, col4 = st.columns(2)
        # with col3:
        input_image = st.file_uploader(
            "Upload an image to use image2image instead (optional)", type=["png", "jpg", "jpeg"]
        )
        if input_image is not None:
            input_image = Image.open(input_image)
            pipeline_name = "img2img"
            # display image using html
            # convert image to base64
            # st.markdown(f"<img src='data:image/png;base64,{input_image}' width='200'>", unsafe_allow_html=True)
            # st.image(input_image, use_column_width=True)
            with tempfile.TemporaryDirectory() as tmpdir:
                gallery_images = []
                input_image.save(os.path.join(tmpdir, "img.png"))
                with open(os.path.join(tmpdir, "img.png"), "rb") as img:
                    encoded = base64.b64encode(img.read()).decode()
                    gallery_images.append(f"data:image/jpeg;base64,{encoded}")

                _ = clickable_images(
                    gallery_images,
                    titles=[f"Image #{str(i)}" for i in range(len(gallery_images))],
                    div_style={"display": "flex", "justify-content": "center", "flex-wrap": "wrap"},
                    img_style={"margin": "5px", "height": "200px"},
                )
        else:
            pipeline_name = "text2img"
        # prompt = st.text_area("Prompt", "Blue elephant")
        # negative_prompt = st.text_area("Negative Prompt", "")
        # with col4:
        col1, col2 = st.columns(2)
        with col1:
            prompt = st.text_area("Prompt", "Blue elephant")
        with col2:
            negative_prompt = st.text_area("Negative Prompt", "")
        # sidebar options
        scheduler = st.sidebar.selectbox("Scheduler", available_schedulers, index=0)
        image_height = st.sidebar.slider("Image height (ignored for img2img)", 128, 1024, 512, 128)
        image_width = st.sidebar.slider("Image width (ignored for img2img)", 128, 1024, 512, 128)
        guidance_scale = st.sidebar.slider("Guidance scale", 1.0, 40.0, 7.5, 0.5)
        strength = st.sidebar.slider("Strength (ignored for text2img)", 0.0, 1.0, 0.8, 0.05)
        num_images = st.sidebar.slider("Number of images per prompt", 1, 30, 1, 1)
        steps = st.sidebar.slider("Steps", 1, 150, 50, 1)

        seed = st.sidebar.number_input("Seed", value=42, min_value=1, max_value=999999, step=1)
        sub_col, download_col = st.columns(2)
        with sub_col:
            submit = st.button("Generate")

        if submit:
            with st.spinner("Generating images..."):
                if pipeline_name == "text2img":
                    output_images, metadata = self.text2img_generate(
                        prompt=prompt,
                        negative_prompt=negative_prompt,
                        scheduler=scheduler,
                        image_size=(image_height, image_width),
                        num_images=num_images,
                        guidance_scale=guidance_scale,
                        steps=steps,
                        seed=seed,
                    )
                elif pipeline_name == "img2img":
                    output_images, metadata = self.img2img_generate(
                        prompt=prompt,
                        image=input_image,
                        strength=strength,
                        negative_prompt=negative_prompt,
                        scheduler=scheduler,
                        num_images=num_images,
                        guidance_scale=guidance_scale,
                        steps=steps,
                        seed=seed,
                    )
            utils.display_and_download_images(output_images, metadata, download_col)
