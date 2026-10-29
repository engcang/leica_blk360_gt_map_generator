/***********************************************************************
 * Software License Agreement (BSD License)
 *
 * Copyright 2008-2009  Marius Muja (mariusm@cs.ubc.ca). All rights reserved.
 * Copyright 2008-2009  David G. Lowe (lowe@cs.ubc.ca). All rights reserved.
 * Copyright 2011-2021  Jose Luis Blanco (joseluisblancoc@gmail.com).
 *   All rights reserved.
 *
 * THE BSD LICENSE
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
 * IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES
 * OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
 * IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY DIRECT, INDIRECT,
 * INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
 * NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
 * DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
 * THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
 * (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF
 * THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *************************************************************************/

// Compact immutable storage for a nanoflann-built 3D tree.
// Search follows nanoflann 1.3.2 traversal; see its BSD notice above.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

template<class Tree> class PackedKdTree {
    struct Node {
        std::uint32_t first, second;  // Children, or the half-open leaf range.
        float low, high;              // Exact extrema of original float32 XYZ.
        std::uint32_t axis;           // 3 marks a leaf.
    };
    Tree &builder;
    std::vector<Node> nodes;

    static std::size_t node_count(typename Tree::NodePtr node) {
        return node ? 1+node_count(node->child1)+node_count(node->child2) : 0;
    }
    std::uint32_t pack(typename Tree::NodePtr source) {
        const auto at = static_cast<std::uint32_t>(nodes.size());
        nodes.push_back({});
        if (!source->child1 && !source->child2) {
            nodes[at] = {std::uint32_t(source->node_type.lr.left),
                         std::uint32_t(source->node_type.lr.right),0,0,3};
        } else {
            const auto &split = source->node_type.sub;
            const float low = float(split.divlow), high = float(split.divhigh);
            if (double(low) != split.divlow || double(high) != split.divhigh)
                throw std::runtime_error("Packed map bounds must preserve original coordinates exactly");
            const auto left = pack(source->child1), right = pack(source->child2);
            nodes[at] = {left,right,low,high,std::uint32_t(split.divfeat)};
        }
        return at;
    }

    template<class Result> void visit(std::uint32_t at, const double *p,
            double bound, std::array<double,3> &axes, Result &result) const {
        const auto &node = nodes[at];
        if (node.axis == 3) {
            const double worst = result.worstDist();
            for (auto i = node.first; i < node.second; ++i) {
                const auto id = builder.vind[i];
                const double distance = builder.distance.evalMetric(p,id,3);
                if (distance < worst) result.addPoint(distance,id);
            }
            return;
        }
        const auto axis = node.axis;
        const double low = p[axis]-double(node.low), high = p[axis]-double(node.high);
        const bool left = low+high < 0;
        const auto near = left ? node.first : node.second, far = left ? node.second : node.first;
        const double delta = left ? high : low, cut = delta*delta;
        visit(near,p,bound,axes,result);
        const double saved = axes[axis], next = bound+cut-saved;
        axes[axis] = cut;
        if (next <= result.worstDist()) visit(far,p,next,axes,result);
        axes[axis] = saved;
    }
public:
    explicit PackedKdTree(Tree &tree) : builder(tree) {
        const auto count = node_count(tree.root_node);
        if (count >= std::numeric_limits<std::uint32_t>::max())
            throw std::length_error("Map tile has too many search nodes");
        nodes.reserve(count);
        if (count) pack(tree.root_node);
        tree.freeIndex(tree);  // Retain only its point permutation and bounding box.
    }
    std::size_t bytes() const { return nodes.capacity()*sizeof(Node); }
    template<class Result> void search(Result &result, const double *p) const {
        if (nodes.empty()) return;
        std::array<double,3> axes{};
        const double bound = builder.computeInitialDistances(builder,p,axes);
        // The immutable root bounds include every original point in this tile.
        // A rounding allowance keeps the exclusion conservative at boundaries.
        const double margin = 128*std::numeric_limits<double>::epsilon()*
            (1+std::abs(p[0])+std::abs(p[1])+std::abs(p[2])+std::sqrt(result.worstDist()));
        if (std::sqrt(bound) > std::sqrt(result.worstDist())+margin) return;
        visit(0,p,bound,axes,result);
    }
};
