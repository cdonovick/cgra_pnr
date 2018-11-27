from __future__ import print_function

from util import reduce_cluster_graph, compute_centroids
from util import SetEncoder, choose_resource
import os
import random
import pythunder
import json
import threading


def detailed_placement_thunder(args, context=None):
    blks = list(args["clusters"])
    cells = args["cells"]
    netlist = args["new_netlist"]
    blk_pos = args["blk_pos"]
    fold_reg = args["fold_reg"]
    # seed = args["seed"]
    # disallowed_pos = args["disallowed_pos"]
    clb_type = args["clb_type"]
    fixed_pos = {}
    for blk_id in blk_pos:
        fixed_pos[blk_id] = list(blk_pos[blk_id])
    placer = pythunder.DetailedPlacer(blks, netlist, cells,
                                      fixed_pos, clb_type,
                                      fold_reg)
    placer.anneal()
    placer.refine(1000, 0.01, False)
    placement = placer.realize()
    keys_to_remove = set()
    for blk_id in placement:
        if blk_id[0] == "x":
            keys_to_remove.add(blk_id)
    for blk_id in keys_to_remove:
        placement.pop(blk_id, None)
    if context is None:
        return placement
    else:
        return {'statusCode': 200,
                'headers': {'Content-Type': 'application/json'},
                'body': placement
                }


def estimate_placement_time(args):
    blks = list(args["clusters"])
    cells = args["cells"]
    netlist = args["new_netlist"]
    blk_pos = args["blk_pos"]
    fold_reg = args["fold_reg"]
    clb_type = args["clb_type"]
    fixed_pos = {}
    for blk_id in blk_pos:
        fixed_pos[blk_id] = list(blk_pos[blk_id])
    new_cells = {}
    for blk_type in cells:
        new_cells[blk_type] = list(cells[blk_type])
    placer = pythunder.DetailedPlacer(blks, netlist, new_cells,
                                      fixed_pos, clb_type,
                                      fold_reg)
    t = placer.estimate(10000)
    return t


def get_lambda_arn(map_args, aws_config):
    from six.moves import queue
    threads = []
    que = queue.Queue()
    for i in range(len(map_args)):
        t = threading.Thread(target=lambda q, arg, index: q.put(
            (index, estimate_placement_time(arg))), args=(que, map_args[i], i))
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    estimates = [-1] * len(map_args)
    while not que.empty():
        index, estimate = que.get()
        estimates[index] = estimate

    for t in estimates:
        assert t != -1
    return choose_resource(estimates, aws_config)


def refine_global_thunder(board_meta, pre_placement, netlists, fixed_pos,
                          fold_reg):
    board_layout = board_meta[0]
    board_info = board_meta[-1]
    clb_type = board_info["clb_type"]
    available_pos = {}
    for y in range(len(board_layout)):
        for x in range(len(board_layout[y])):
            blk_type = board_layout[y][x]
            if blk_type is not None:
                if blk_type not in available_pos:
                    available_pos[blk_type] = []
                available_pos[blk_type].append((x, y))
    global_refine = pythunder.DetailedPlacer(pre_placement,
                                             netlists,
                                             available_pos,
                                             fixed_pos,
                                             clb_type,
                                             fold_reg)

    if "TRAVIS" not in os.environ:
        # FIXME: travis hack
        # remove this after new router
        global_refine.refine(int(10 * (len(pre_placement) ** 1.33)),
                             0.01, True)

    return global_refine.realize()


def main():
    # only the main thread needs it
    import numpy as np
    from argparse import ArgumentParser
    from arch.parser import parse_emb
    from arch import make_board, parse_cgra, generate_place_on_board, parse_fpga
    from arch.cgra import place_special_blocks, save_placement, prune_netlist
    from arch.cgra_packer import load_packed_file
    from arch.fpga import load_packed_fpga_netlist
    from arch import mock_board_meta
    from visualize import visualize_placement_cgra

    parser = ArgumentParser("CGRA Placer")
    parser.add_argument("-i", "--input", help="Packed netlist file, " +
                                              "e.g. harris.packed",
                        required=True, action="store", dest="packed_filename")
    parser.add_argument("-e", "--embedding", help="Netlist embedding file, " +
                                                  "e.g. harris.emb",
                        required=True, action="store", dest="netlist_embedding")
    parser.add_argument("-o", "--output", help="Placement result, " +
                                               "e.g. harris.place",
                        required=True, action="store",
                        dest="placement_filename")
    parser.add_argument("-c", "--cgra", help="CGRA architecture file",
                        action="store", dest="cgra_arch", default="")
    parser.add_argument("--no-reg-fold", help="If set, the placer will treat " +
                                              "registers as PE tiles",
                        action="store_true",
                        required=False, dest="no_reg_fold", default=False)
    parser.add_argument("--no-vis", help="If set, the placer won't show " +
                                         "visualization result for placement",
                        action="store_true",
                        required=False, dest="no_vis", default=False)
    parser.add_argument("-s", "--seed", help="Seed for placement. " +
                                             "default is 0", type=int,
                        default=0,
                        required=False, action="store", dest="seed")

    parser.add_argument("-a", "--aws", help="Serverless configuration for " +
                        "detailed placement. If set, will try to connect to "
                        "that arn",
                        dest="aws_config", type=str, required=False,
                        action="store", default="")
    parser.add_argument("-f", "--fpga", action="store", dest="fpga_arch",
                        default="", help="ISPD FPGA architecture file")
    parser.add_argument("--mock", action="store", dest="mock_size",
                        default=0, type=int, help="Mock CGRA board with "
                                                  "provided size")
    args = parser.parse_args()

    cgra_arch = args.cgra_arch
    fpga_arch = args.fpga_arch
    mock_size = args.mock_size

    if len(cgra_arch) == 0 ^ len(fpga_arch) == 0 and mock_size == 0:
        parser.error("Must provide wither --fpga or --cgra")

    packed_filename = args.packed_filename
    netlist_embedding = args.netlist_embedding
    placement_filename = args.placement_filename
    aws_config = args.aws_config
    fpga_place = len(fpga_arch) > 0

    seed = args.seed
    print("Using seed", seed, "for placement")
    # just in case for some library
    random.seed(seed)
    np.random.seed(seed)

    vis_opt = not args.no_vis
    fold_reg = not args.no_reg_fold
    # FPGA params override
    if mock_size > 0:
        fold_reg = False
        board_meta = mock_board_meta(mock_size)
    elif fpga_place:
        fold_reg = False
        board_meta = parse_fpga(fpga_arch)
    else:
        board_meta = parse_cgra(cgra_arch, fold_reg=fold_reg)
    print(fold_reg)
    # Common routine
    board_name, board_meta = board_meta.popitem()
    print("INFO: Placing for", board_name)
    num_dim, raw_emb = parse_emb(netlist_embedding)
    board = make_board(board_meta)
    board_info = board_meta[-1]
    place_on_board = generate_place_on_board(board_meta, fold_reg=fold_reg)

    fixed_blk_pos = {}
    special_blocks = set()
    emb = {}

    # FPGA
    if fpga_place:
        netlists, fixed_blk_pos, _ = load_packed_fpga_netlist(packed_filename)
        num_of_kernels = None
        id_to_name = {}
        for blk_id in raw_emb:
            id_to_name[blk_id] = blk_id
            if blk_id[0] == "i":
                special_blocks.add(blk_id)
            else:
                emb[blk_id] = raw_emb[blk_id]
        # place fixed IO locations
        for blk_id in fixed_blk_pos:
            pos = fixed_blk_pos[blk_id]
            place_on_board(board, blk_id, pos)

        folded_blocks = {}
        changed_pe = {}

    else:
        # CGRA
        raw_netlist, folded_blocks, id_to_name, changed_pe = \
            load_packed_file(packed_filename)
        num_of_kernels = get_num_clusters(id_to_name)
        netlists = prune_netlist(raw_netlist)

        for blk_id in raw_emb:
            if blk_id[0] == "i":
                special_blocks.add(blk_id)
            else:
                emb[blk_id] = raw_emb[blk_id]
        # place the spacial blocks first
        place_special_blocks(board, special_blocks, fixed_blk_pos, raw_netlist,
                             id_to_name,
                             place_on_board,
                             board_meta)

    # common routine
    data_x = np.zeros((len(emb), num_dim))
    blks = list(emb.keys())
    for i in range(len(blks)):
        data_x[i] = emb[blks[i]]

    centroids, cluster_cells, clusters = perform_global_placement(
        blks, data_x, emb, fixed_blk_pos, netlists,
        board_meta, fold_reg=fold_reg, num_clusters=num_of_kernels,
        seed=seed, fpga_place=fpga_place, vis=vis_opt)

    # placer with each cluster
    board_pos = perform_detailed_placement(centroids,
                                           cluster_cells, clusters,
                                           fixed_blk_pos, netlists,
                                           fold_reg, seed,
                                           board_info,
                                           aws_config)
    # refinement
    board_pos = refine_global_thunder(board_meta, board_pos, netlists,
                                      fixed_blk_pos, fold_reg)

    for blk_id in board_pos:
        pos = board_pos[blk_id]
        place_on_board(board, blk_id, pos)

    # save the placement file
    save_placement(board_pos, id_to_name, folded_blocks, placement_filename)
    basename_file = os.path.basename(placement_filename)
    design_name, _ = os.path.splitext(basename_file)
    if vis_opt:
        visualize_placement_cgra(board_meta, board_pos, design_name, changed_pe)


def get_num_clusters(id_to_name):
    unique_names = set()
    for blk_id in id_to_name:
        blk_name = id_to_name[blk_id]
        name = blk_name.split(".")[0]
        name = name.split("$")[0]
        unique_names.add(name)

    count = [1 for name in unique_names if name[:2] == "lb" and
             "lut" not in name]
    return sum(count)


def perform_global_placement(blks, data_x, emb, fixed_blk_pos, netlists,
                             board_meta, fold_reg, seed,
                             num_clusters=None, fpga_place=False, vis=True):
    from sklearn.cluster import KMeans
    import numpy as np
    from visualize import visualize_clustering_cgra
    # simple heuristics to calculate the clusters
    if fpga_place:
        num_clusters = int(np.ceil(len(emb) / 300)) + 1
    elif num_clusters is None or num_clusters == 0:
        num_clusters = int(np.ceil(len(emb) / 40)) + 1
    # extra careful
    num_clusters = min(num_clusters, len(blks))
    print("Trying: num of clusters", num_clusters)
    kmeans = KMeans(n_clusters=num_clusters, random_state=0).fit(data_x)
    cluster_ids = kmeans.labels_
    clusters = {}
    for i in range(len(blks)):
        cid = cluster_ids[i]
        if cid not in clusters:
            clusters[cid] = {blks[i]}
        else:
            clusters[cid].add(blks[i])
    cluster_sizes = [len(clusters[s]) for s in clusters]
    print("cluster average:", np.average(cluster_sizes), "std:",
          np.std(cluster_sizes), "total:", np.sum(cluster_sizes))
    new_clusters = {}
    for c_id in clusters:
        new_id = "x" + str(c_id)
        new_clusters[new_id] = set()
        for blk in clusters[c_id]:
            # make sure that fixed blocks are not in the clusters
            if blk not in fixed_blk_pos:
                new_clusters[new_id].add(blk)

    # prepare for the input
    new_layout = []
    board_layout = board_meta[0]
    for y in range(len(board_layout)):
        row = []
        for x in range(len(board_layout[y])):
            if board_layout[y][x] is None:
                row.append(' ')
            else:
                row.append(board_layout[y][x])
        new_layout.append(row)

    board_info = board_meta[-1]
    clb_type = board_info["clb_type"]
    print(fold_reg)
    gp = pythunder.GlobalPlacer(new_clusters, netlists, fixed_blk_pos,
                                new_layout, clb_type, fold_reg)

    gp.solve()
    gp.anneal()
    cluster_cells_ = gp.realize()

    cluster_cells = {}
    for c_id in cluster_cells_:
        cells = cluster_cells_[c_id]
        c_id = int(c_id[1:])
        cluster_cells[c_id] = cells
    centroids = compute_centroids(cluster_cells, b_type=clb_type)

    if vis:
        visualize_clustering_cgra(board_meta, cluster_cells)
    assert (cluster_cells is not None and centroids is not None)
    return centroids, cluster_cells, clusters


def detailed_placement_thunder_wrapper(args):
    clusters = {}
    cells = {}
    netlists = {}
    fixed_blocks = {}
    clb_type = args[0]["clb_type"]
    fold_reg = args[0]["fold_reg"]
    for i in range(len(args)):
        arg = args[i]
        clusters[i] = arg["clusters"]
        cells[i] = arg["cells"]
        netlists[i] = arg["new_netlist"]
        fixed_blocks[i] = arg["blk_pos"]
    return pythunder.detailed_placement(clusters, cells, netlists, fixed_blocks,
                                        clb_type,
                                        fold_reg)


def perform_detailed_placement(centroids, cluster_cells, clusters,
                               fixed_blk_pos, netlists,
                               fold_reg, seed, board_info,
                               aws_config=""):
    from six.moves import queue
    import boto3
    board_pos = fixed_blk_pos.copy()
    map_args = []

    # NOTE:
    # This is CGRA only. there are corner cases where the reg will put
    # into one of the corner and the main net routes through all the available
    # channels. Hence it becomes not routable any more
    height, width = board_info["height"], board_info["width"]
    margin = board_info["margin"]
    clb_type = board_info["clb_type"]
    disallowed_pos = [(margin, margin), (margin, margin + height),
                      (margin + width, margin),
                      (margin + width, margin + height)]

    for c_id in cluster_cells:
        cells = cluster_cells[c_id]
        new_netlist = reduce_cluster_graph(netlists, clusters,
                                           fixed_blk_pos, c_id)
        blk_pos = fixed_blk_pos.copy()
        for i in centroids:
            if i == c_id:
                continue
            node_id = "x" + str(i)
            pos = centroids[i]
            blk_pos[node_id] = pos
        args = {"clusters": clusters[c_id], "cells": cells,
                "new_netlist": new_netlist,
                "blk_pos": blk_pos, "fold_reg": fold_reg,
                "seed": seed, "clb_type": clb_type,
                "disallowed_pos": disallowed_pos}
        map_args.append(args)
    if not aws_config:
        return detailed_placement_thunder_wrapper(map_args)
    else:
        # user need to specify a region in the environment
        client = boto3.client("lambda")
        import time
        threads = []
        lambda_arns = get_lambda_arn(map_args, aws_config)
        que = queue.Queue()
        lambda_res = {}
        start = time.time()
        for i in range(len(map_args)):
            t = threading.Thread(target=lambda q, arg, arn:
            q.put(client.invoke(
                **{"FunctionName": arn,
                   "InvocationType": "RequestResponse",
                   "Payload":
                       bytes(json.dumps(arg, cls=SetEncoder))})
                  ["Payload"].read()),
                                 args=(que, map_args[i], lambda_arns[i][1]))
            threads.append(t)
            lambda_res[i] = lambda_arns[i][0]
        # sort the threads so that the ones needs most resources runs first
        # this gives us some spaces for mis-calculated runtime approximation
        index_list = list(range(len(map_args)))
        index_list.sort(key=lambda x: lambda_res[x], reverse=True)
        # start
        for i in index_list:
            t = threads[i]
            t.start()
        # skip join, use blocking while loop to aggressively waiting threads
        # to finish
        job_count = 0
        # merge
        while job_count < len(map_args):
            if not que.empty():
                res = json.loads(que.get())
                r = res["body"]
                board_pos.update(r)
                job_count += 1
        end = time.time()
        print("Lambda takes", end - start, "seconds")
        return board_pos


if __name__ == "__main__":
    main()
