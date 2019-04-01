/*
 * Copyright 2016 GoPro Inc.
 *
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <limits.h>

#include "buffer.h"
#include "format.h"
#include "glincludes.h"
#include "hmap.h"
#include "log.h"
#include "math_utils.h"
#include "memory.h"
#include "nodegl.h"
#include "nodes.h"
#include "utils.h"

#define TEXTURES_TYPES_LIST (const int[]){NGL_NODE_TEXTURE2D,       \
                                          NGL_NODE_TEXTURE3D,       \
                                          NGL_NODE_TEXTURECUBE,     \
                                          -1}

#define PROGRAMS_TYPES_LIST (const int[]){NGL_NODE_PROGRAM,         \
                                          -1}

#define UNIFORMS_TYPES_LIST (const int[]){NGL_NODE_BUFFERFLOAT,     \
                                          NGL_NODE_BUFFERVEC2,      \
                                          NGL_NODE_BUFFERVEC3,      \
                                          NGL_NODE_BUFFERVEC4,      \
                                          NGL_NODE_UNIFORMFLOAT,    \
                                          NGL_NODE_UNIFORMVEC2,     \
                                          NGL_NODE_UNIFORMVEC3,     \
                                          NGL_NODE_UNIFORMVEC4,     \
                                          NGL_NODE_UNIFORMQUAT,     \
                                          NGL_NODE_UNIFORMINT,      \
                                          NGL_NODE_UNIFORMMAT4,     \
                                          -1}

#define ATTRIBUTES_TYPES_LIST (const int[]){NGL_NODE_BUFFERFLOAT,   \
                                            NGL_NODE_BUFFERVEC2,    \
                                            NGL_NODE_BUFFERVEC3,    \
                                            NGL_NODE_BUFFERVEC4,    \
                                            NGL_NODE_BUFFERMAT4,    \
                                            -1}

#define GEOMETRY_TYPES_LIST (const int[]){NGL_NODE_CIRCLE,          \
                                          NGL_NODE_GEOMETRY,        \
                                          NGL_NODE_QUAD,            \
                                          NGL_NODE_TRIANGLE,        \
                                          -1}

#define OFFSET(x) offsetof(struct render_priv, x)
static const struct node_param render_params[] = {
    {"geometry", PARAM_TYPE_NODE, OFFSET(geometry), .flags=PARAM_FLAG_CONSTRUCTOR,
                 .node_types=GEOMETRY_TYPES_LIST,
                 .desc=NGLI_DOCSTRING("geometry to be rasterized")},
    {"program",  PARAM_TYPE_NODE, OFFSET(program),
                 .node_types=PROGRAMS_TYPES_LIST,
                 .desc=NGLI_DOCSTRING("program to be executed")},
    {"textures", PARAM_TYPE_NODEDICT, OFFSET(textures),
                 .node_types=TEXTURES_TYPES_LIST,
                 .desc=NGLI_DOCSTRING("textures made accessible to the `program`")},
    {"uniforms", PARAM_TYPE_NODEDICT, OFFSET(uniforms),
                 .node_types=UNIFORMS_TYPES_LIST,
                 .desc=NGLI_DOCSTRING("uniforms made accessible to the `program`")},
    {"blocks",  PARAM_TYPE_NODEDICT, OFFSET(blocks),
                 .node_types=(const int[]){NGL_NODE_BLOCK, -1},
                 .desc=NGLI_DOCSTRING("blocks made accessible to the `program`")},
    {"attributes", PARAM_TYPE_NODEDICT, OFFSET(attributes),
                 .node_types=ATTRIBUTES_TYPES_LIST,
                 .desc=NGLI_DOCSTRING("extra vertex attributes made accessible to the `program`")},
    {"instance_attributes", PARAM_TYPE_NODEDICT, OFFSET(instance_attributes),
                 .node_types=ATTRIBUTES_TYPES_LIST,
                 .desc=NGLI_DOCSTRING("per instance extra vertex attributes made accessible to the `program`")},
    {"nb_instances", PARAM_TYPE_INT, OFFSET(nb_instances),
                 .desc=NGLI_DOCSTRING("number of instances to draw")},
    {NULL}
};

static void update_vertex_attribs_from_pairs(struct glcontext *gl,
                                             const struct darray *attribute_pairs,
                                             int is_instance_attrib)
{
    const struct nodeprograminfopair *pairs = ngli_darray_data(attribute_pairs);
    for (int i = 0; i < ngli_darray_count(attribute_pairs); i++) {
        const struct nodeprograminfopair *pair = &pairs[i];
        const struct ngl_node *node = pair->node;
        const struct attributeprograminfo *info = pair->program_info;
        const GLint index = info->location;
        struct buffer_priv *buffer = pair->node->priv_data;

        /* TODO: check that attribute type is mat4 */
        const int nb_attribs = node->class->id == NGL_NODE_BUFFERMAT4 ? 4 : 1;
        const int attrib_comp = buffer->data_comp / nb_attribs;
        uintptr_t attrib_stride = buffer->data_stride / nb_attribs;
        uintptr_t data_stride = buffer->data_stride;
        int buffer_id = buffer->buffer.id;
        int offset = 0;

        if (buffer->block) {
            const struct block_priv *block = buffer->block->priv_data;
            const struct block_field_info *fi = &block->field_info[buffer->block_field];
            attrib_stride = fi->stride / nb_attribs;
            data_stride = fi->stride;
            buffer_id = block->buffer.id;
            offset = fi->offset;
        }

        ngli_glEnableVertexAttribArray(gl, index);
        ngli_glBindBuffer(gl, GL_ARRAY_BUFFER, buffer_id);

        for (int j = 0; j < nb_attribs; j++) {
            ngli_glEnableVertexAttribArray(gl, index + j);
            ngli_glVertexAttribPointer(gl, index + j, attrib_comp, GL_FLOAT, GL_FALSE, data_stride, (void *)(j * attrib_stride + offset));
        }

        if (is_instance_attrib) {
            for (int j = 0; j < nb_attribs; j++)
                ngli_glVertexAttribDivisor(gl, index + j, 1);
        }
    }
}

static void update_vertex_attribs(struct ngl_node *node)
{
    struct ngl_ctx *ctx = node->ctx;
    struct glcontext *gl = ctx->glcontext;
    struct render_priv *s = node->priv_data;

    update_vertex_attribs_from_pairs(gl, &s->builtin_attribute_pairs, 0);
    update_vertex_attribs_from_pairs(gl, &s->attribute_pairs, 0);
    update_vertex_attribs_from_pairs(gl, &s->instance_attribute_pairs, 1);
}

static void disable_vertex_attribs_from_pairs(struct glcontext *gl,
                                              const struct darray *attribute_pairs)
{
    const struct nodeprograminfopair *pairs = ngli_darray_data(attribute_pairs);
    for (int i = 0; i < ngli_darray_count(attribute_pairs); i++) {
        const struct nodeprograminfopair *pair = &pairs[i];
        const struct attributeprograminfo *info = pair->program_info;
        const struct ngl_node *node = pair->node;
        const int nb_attribs = node->class->id == NGL_NODE_BUFFERMAT4 ? 4 : 1;
        for (int i = 0; i < nb_attribs; i++) {
            ngli_glDisableVertexAttribArray(gl, info->location + i);
            if (gl->features & NGLI_FEATURE_INSTANCED_ARRAY)
                ngli_glVertexAttribDivisor(gl, info->location + i, 0);
        }
    }
}

static void disable_vertex_attribs(struct ngl_node *node)
{
    struct ngl_ctx *ctx = node->ctx;
    struct glcontext *gl = ctx->glcontext;
    struct render_priv *s = node->priv_data;

    disable_vertex_attribs_from_pairs(gl, &s->builtin_attribute_pairs);
    disable_vertex_attribs_from_pairs(gl, &s->attribute_pairs);
    disable_vertex_attribs_from_pairs(gl, &s->instance_attribute_pairs);
}

static int pair_node_to_attribinfo(struct render_priv *s,
                                   struct darray *attribute_pairs,
                                   const char *name,
                                   struct ngl_node *anode)
{
    const struct ngl_node *pnode = s->program;
    const struct program_priv *program = pnode->priv_data;
    const struct attributeprograminfo *active_attribute =
        ngli_hmap_get(program->active_attributes, name);
    if (!active_attribute)
        return 1;

    if (active_attribute->location < 0)
        return 0;

    int ret = ngli_node_buffer_ref(anode);
    if (ret < 0)
        return ret;

    struct nodeprograminfopair pair = {
        .node = anode,
        .program_info = (void *)active_attribute,
    };
    snprintf(pair.name, sizeof(pair.name), "%s", name);
    if (!ngli_darray_push(attribute_pairs, &pair)) {
        ngli_node_buffer_unref(anode);
        return -1;
    }

    return 0;
}

static int pair_nodes_to_attribinfo(struct ngl_node *node,
                                    struct darray *attribute_pairs,
                                    struct hmap *attributes,
                                    int per_instance,
                                    int warn_not_found)
{
    if (!attributes)
        return 0;

    struct render_priv *s = node->priv_data;

    const struct hmap_entry *entry = NULL;
    while ((entry = ngli_hmap_next(attributes, entry))) {
        struct ngl_node *anode = entry->data;
        struct buffer_priv *buffer = anode->priv_data;

        if (per_instance) {
            if (buffer->count != s->nb_instances) {
                LOG(ERROR,
                    "attribute buffer %s count (%d) does not match instance count (%d)",
                    entry->key,
                    buffer->count,
                    s->nb_instances);
                return -1;
            }
        } else {
            struct geometry_priv *geometry = s->geometry->priv_data;
            struct buffer_priv *vertices = geometry->vertices_buffer->priv_data;
            if (buffer->count != vertices->count) {
                LOG(ERROR,
                    "attribute buffer %s count (%d) does not match vertices count (%d)",
                    entry->key,
                    buffer->count,
                    vertices->count);
                return -1;
            }
        }

        int ret = pair_node_to_attribinfo(s, attribute_pairs, entry->key, anode);
        if (ret < 0)
            return ret;

        if (warn_not_found && ret == 1) {
            const struct ngl_node *pnode = s->program;
            LOG(WARNING, "attribute %s attached to %s not found in %s",
                entry->key, node->label, pnode->label);
        }
    }
    return 0;
}

static void draw_elements(struct glcontext *gl, struct render_priv *render)
{
    struct geometry_priv *geometry = render->geometry->priv_data;
    const struct buffer_priv *indices = geometry->indices_buffer->priv_data;
    ngli_glBindBuffer(gl, GL_ELEMENT_ARRAY_BUFFER, indices->buffer.id);
    ngli_glDrawElements(gl, geometry->topology, indices->count, render->indices_type, 0);
}

static void draw_elements_instanced(struct glcontext *gl, struct render_priv *render)
{
    struct geometry_priv *geometry = render->geometry->priv_data;
    struct buffer_priv *indices = geometry->indices_buffer->priv_data;
    ngli_glBindBuffer(gl, GL_ELEMENT_ARRAY_BUFFER, indices->buffer.id);
    ngli_glDrawElementsInstanced(gl, geometry->topology, indices->count, render->indices_type, 0, render->nb_instances);
}

static void draw_arrays(struct glcontext *gl, struct render_priv *render)
{
    struct geometry_priv *geometry = render->geometry->priv_data;
    struct buffer_priv *vertices = geometry->vertices_buffer->priv_data;
    ngli_glDrawArrays(gl, geometry->topology, 0, vertices->count);
}

static void draw_arrays_instanced(struct glcontext *gl, struct render_priv *render)
{
    struct geometry_priv *geometry = render->geometry->priv_data;
    struct buffer_priv *vertices = geometry->vertices_buffer->priv_data;
    ngli_glDrawArraysInstanced(gl, geometry->topology, 0, vertices->count, render->nb_instances);
}

#define GEOMETRY_OFFSET(x) offsetof(struct geometry_priv, x)
static const struct {
    const char *const_name;
    int offset;
} attrib_const_map[] = {
    {"ngl_position", GEOMETRY_OFFSET(vertices_buffer)},
    {"ngl_uvcoord",  GEOMETRY_OFFSET(uvcoords_buffer)},
    {"ngl_normal",   GEOMETRY_OFFSET(normals_buffer)},
};

static int init_builtin_attributes(struct render_priv *s)
{
    s->builtin_attributes = ngli_hmap_create();
    if (!s->builtin_attributes)
        return -1;

    struct geometry_priv *geometry = s->geometry->priv_data;
    for (int i = 0; i < NGLI_ARRAY_NB(attrib_const_map); i++) {
        const int offset = attrib_const_map[i].offset;
        const char *const_name = attrib_const_map[i].const_name;
        uint8_t *buffer_node_p = ((uint8_t *)geometry) + offset;
        struct ngl_node *anode = *(struct ngl_node **)buffer_node_p;
        if (!anode)
            continue;

        int ret = ngli_hmap_set(s->builtin_attributes, const_name, anode);
        if (ret < 0)
            return ret;
    }
    return 0;
}

static int render_init(struct ngl_node *node)
{
    struct ngl_ctx *ctx = node->ctx;
    struct glcontext *gl = ctx->glcontext;
    struct render_priv *s = node->priv_data;

    if (!s->program) {
        s->program = ngl_node_create(NGL_NODE_PROGRAM);
        if (!s->program)
            return -1;
        int ret = ngli_node_attach_ctx(s->program, ctx);
        if (ret < 0)
            return ret;
    }

    struct pipeline_params params = {
        .label = node->label,
        .program = s->program,
        .textures = s->textures,
        .uniforms = s->uniforms,
        .blocks = s->blocks,
    };
    int ret = ngli_pipeline_init(&s->pipeline, ctx, &params);
    if (ret < 0)
        return ret;

    /* Instancing checks */
    if (s->nb_instances && !(gl->features & NGLI_FEATURE_DRAW_INSTANCED)) {
        LOG(ERROR, "context does not support instanced draws");
        return -1;
    }

    if (s->instance_attributes && !(gl->features & NGLI_FEATURE_INSTANCED_ARRAY)) {
        LOG(ERROR, "context does not support instanced arrays");
        return -1;
    }

    /* User and builtin attribute pairs */
    ngli_darray_init(&s->builtin_attribute_pairs, sizeof(struct nodeprograminfopair), 0);
    ngli_darray_init(&s->attribute_pairs, sizeof(struct nodeprograminfopair), 0);
    ngli_darray_init(&s->instance_attribute_pairs, sizeof(struct nodeprograminfopair), 0);

    ret = init_builtin_attributes(s);
    if (ret < 0)
        return ret;

    /* Builtin vertex attributes */
    ret = pair_nodes_to_attribinfo(node, &s->builtin_attribute_pairs, s->builtin_attributes, 0, 0);
    if (ret < 0)
        return ret;

    /* User vertex attributes */
    ret = pair_nodes_to_attribinfo(node, &s->attribute_pairs, s->attributes, 0, 1);
    if (ret < 0)
        return ret;

    /* User per instance vertex attributes */
    ret = pair_nodes_to_attribinfo(node, &s->instance_attribute_pairs, s->instance_attributes, 1, 1);
    if (ret < 0)
        return ret;

    struct geometry_priv *geometry = s->geometry->priv_data;
    if (geometry->indices_buffer) {
        ret = ngli_node_buffer_ref(geometry->indices_buffer);
        if (ret < 0)
            return ret;
        s->has_indices_buffer_ref = 1;

        struct buffer_priv *indices = geometry->indices_buffer->priv_data;
        if (indices->block) {
            LOG(ERROR, "geometry indices buffers referencing a block are not supported");
            return -1;
        }

        ngli_format_get_gl_texture_format(gl, indices->data_format, NULL, NULL, &s->indices_type);
    }

    if (gl->features & NGLI_FEATURE_VERTEX_ARRAY_OBJECT) {
        ngli_glGenVertexArrays(gl, 1, &s->vao_id);
        ngli_glBindVertexArray(gl, s->vao_id);
        update_vertex_attribs(node);
    }

    if (geometry->indices_buffer)
        s->draw = s->nb_instances > 0 ? draw_elements_instanced : draw_elements;
    else
        s->draw = s->nb_instances > 0 ? draw_arrays_instanced : draw_arrays;

    return 0;
}

static void uninit_attributes(struct darray *attribute_pairs)
{
    struct nodeprograminfopair *pairs = ngli_darray_data(attribute_pairs);
    for (int i = 0; i < ngli_darray_count(attribute_pairs); i++) {
        struct nodeprograminfopair *pair = &pairs[i];
        ngli_node_buffer_unref(pair->node);
    }

    ngli_darray_reset(attribute_pairs);
}

static void render_uninit(struct ngl_node *node)
{
    struct ngl_ctx *ctx = node->ctx;
    struct glcontext *gl = ctx->glcontext;
    struct render_priv *s = node->priv_data;

    ngli_hmap_freep(&s->builtin_attributes);

    if (gl->features & NGLI_FEATURE_VERTEX_ARRAY_OBJECT) {
        ngli_glDeleteVertexArrays(gl, 1, &s->vao_id);
    }

    ngli_pipeline_uninit(&s->pipeline);

    if (s->has_indices_buffer_ref) {
        struct geometry_priv *geometry = s->geometry->priv_data;
        ngli_node_buffer_unref(geometry->indices_buffer);
    }

    uninit_attributes(&s->builtin_attribute_pairs);
    uninit_attributes(&s->attribute_pairs);
    uninit_attributes(&s->instance_attribute_pairs);
}

static int update_attributes(struct darray *attribute_pairs, double t)
{
    struct nodeprograminfopair *pairs = ngli_darray_data(attribute_pairs);
    for (int i = 0; i < ngli_darray_count(attribute_pairs); i++) {
        struct nodeprograminfopair *pair = &pairs[i];
        struct ngl_node *bnode = pair->node;
        int ret = ngli_node_update(bnode, t);
        if (ret < 0)
            return ret;
        ret = ngli_node_buffer_upload(bnode);
        if (ret < 0)
            return ret;
    }
    return 0;
}

static int render_update(struct ngl_node *node, double t)
{
    struct render_priv *s = node->priv_data;

    int ret = ngli_node_update(s->geometry, t);
    if (ret < 0)
        return ret;

    ret = update_attributes(&s->builtin_attribute_pairs, t);
    if (ret < 0)
        return ret;

    ret = update_attributes(&s->attribute_pairs, t);
    if (ret < 0)
        return ret;

    ret = update_attributes(&s->instance_attribute_pairs, t);
    if (ret < 0)
        return ret;

    return ngli_pipeline_update(&s->pipeline, t);
}

static void render_draw(struct ngl_node *node)
{
    struct ngl_ctx *ctx = node->ctx;
    struct glcontext *gl = ctx->glcontext;
    struct render_priv *s = node->priv_data;

    ngli_honor_pending_glstate(ctx);

    const struct program_priv *program = s->program->priv_data;
    ngli_glUseProgram(gl, program->program_id);

    if (gl->features & NGLI_FEATURE_VERTEX_ARRAY_OBJECT) {
        ngli_glBindVertexArray(gl, s->vao_id);
    } else {
        update_vertex_attribs(node);
    }

    int ret = ngli_pipeline_upload_data(&s->pipeline);
    if (ret < 0) {
        LOG(ERROR, "pipeline upload data error");
    }

    s->draw(gl, s);

    if (!(gl->features & NGLI_FEATURE_VERTEX_ARRAY_OBJECT)) {
        disable_vertex_attribs(node);
    }
}

const struct node_class ngli_render_class = {
    .id        = NGL_NODE_RENDER,
    .name      = "Render",
    .init      = render_init,
    .uninit    = render_uninit,
    .update    = render_update,
    .draw      = render_draw,
    .priv_size = sizeof(struct render_priv),
    .params    = render_params,
    .file      = __FILE__,
};
