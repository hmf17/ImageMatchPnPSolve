import torch
import torch.nn as nn
from torchvision import transforms
from PIL import Image
from torch.autograd import Variable
from torch.nn.functional import grid_sample
from sys import argv
import numpy as np
import cv2
from math import cos, sin, pi, sqrt
import time

USE_CUDA = torch.cuda.is_available()
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# USE_CUDA = False
# device = 'cpu'

# from trt_trans import get_engine, allocate_buffers, do_inference, postprocess_the_outputs
# build engine
# max_batch_size = 1
# onnx_model_path = '../models/conv.onnx'
# trt_engine_path = '../models/conv.trt'
# fp16_mode = False
# int8_mode = False
# engine = get_engine(max_batch_size, onnx_model_path, trt_engine_path, fp16_mode, int8_mode)
# context = engine.create_execution_context()
# inputs, outputs, bindings, stream = allocate_buffers(engine)
# # print(inputs[0].host.shape)
# # print(outputs[0].host.shape)


class InverseBatch(torch.autograd.Function):

	def forward(self, input):
		batch_size, h, w = input.size()
		assert (h == w)
		H = torch.Tensor(batch_size, h, h).type_as(input)
		for i in range(0, batch_size):
			H[i, :, :] = input[i, :, :].cpu().inverse()
		# self.save_for_backward(H)
		self.H = H
		return H

	def backward(self, grad_output):
		# print(grad_output.is_contiguous())
		# H, = self.saved_tensors
		H = self.H

		[batch_size, h, w] = H.size()
		assert (h == w)
		Hl = H.transpose(1, 2).repeat(1, 1, h).view(batch_size * h * h, h, 1)
		# print(Hl.view(batch_size, h, h, h, 1))
		Hr = H.repeat(1, h, 1).view(batch_size * h * h, 1, h)
		# print(Hr.view(batch_size, h, h, 1, h))

		r = Hl.bmm(Hr).view(batch_size, h, h, h, h) * \
		    grad_output.contiguous().view(batch_size, 1, 1, h, h).expand(batch_size, h, h, h, h)
		# print(r.size())
		return -r.sum(-1).sum(-1)
	# print(r)


def InverseBatchFun(input):
	batch_size, h, w = input.size()
	assert (h == w)
	H = torch.Tensor(batch_size, h, h).type_as(input)
	for i in range(0, batch_size):
		# 这里在Xavier上inverse函数不支持，所以这么改
		H[i, :, :] = input[i, :, :].cpu().inverse()

	return H


class GradientBatch(nn.Module):

	def __init__(self):
		super(GradientBatch, self).__init__()
		wx = torch.FloatTensor([-.5, 0, .5]).view(1, 1, 1, 3)
		wy = torch.FloatTensor([[-.5], [0], [.5]]).view(1, 1, 3, 1)
		self.register_buffer('wx', wx)
		self.register_buffer('wy', wy)
		self.padx_func = torch.nn.ReplicationPad2d((1, 1, 0, 0))
		self.pady_func = torch.nn.ReplicationPad2d((0, 0, 1, 1))

	def forward(self, img):
		batch_size, k, h, w = img.size()
		img_ = img.view(batch_size * k, h, w)
		img_ = img_.unsqueeze(1)

		img_padx = self.padx_func(img_)
		img_dx = torch.nn.functional.conv2d(input=img_padx,
		                                    weight=Variable(self.wx),
		                                    stride=1,
		                                    padding=0).squeeze(1)

		img_pady = self.pady_func(img_)
		img_dy = torch.nn.functional.conv2d(input=img_pady,
		                                    weight=Variable(self.wy),
		                                    stride=1,
		                                    padding=0).squeeze(1)

		img_dx = img_dx.view(batch_size, k, h, w)
		img_dy = img_dy.view(batch_size, k, h, w)

		# if not isinstance(img, torch.autograd.variable.Variable):
		if not isinstance(img, torch.autograd.Variable):
			img_dx = img_dx.data
			img_dy = img_dy.data

		return img_dx, img_dy


def normalize_img_batch(img):
	# per-channel zero-mean and unit-variance of image batch

	# img [in, Tensor N x C x H x W] : batch of images to normalize
	N, C, H, W = img.size()

	# compute per channel mean for batch, subtract from image
	img_vec = img.view(N, C, H * W, 1)
	mean = img_vec.mean(dim=2, keepdim=True)
	img_ = img - mean

	# compute per channel std dev for batch, divide img
	std_dev = img_vec.std(dim=2, keepdim=True)
	img_ = img_ / std_dev

	return img_


def warp_hmg(img, p):
	# 进行patch位置定位的关键所在，
	# 最终计算的是IMG到MAP的一个变化（怎么把IMG上的点变成MAP上的点）
	# perform warping of img batch using homography transform with batch of parameters p
	# img [in, Tensor N x C x H x W] : batch of images to warp
	# p [in, Tensor N x 8 x 1] : batch of warp parameters
	# img_warp [out, Tensor N x C x H x W] : batch of warped images
	# mask [out, Tensor N x H x W] : batch of binary masks indicating valid pixels areas
	batch_size, k, h, w = img.size()

	# # 还是这里存在问题
	# img_np = np.array(img.squeeze(0)).transpose(1, 2, 0)
	# points1 = np.float32([[200, 1300], [400, 1300], [200, 1500], [400, 1500]])
	# points2 = np.float32([[0, 0], [h, 0], [0, w], [h, w]])
	# M = cv2.getPerspectiveTransform(points1, points2)
	# H = param_to_H(p).squeeze(0)
	# H_np = np.linalg.inv(np.array(H))
	# H_np = H_np - [[0,0,h/2],[0,0,w/2],[0,0,0]]
	# # img_warp = cv2.warpPerspective(img_np, H_np, (133, 100), flags=cv2.WARP_INVERSE_MAP)
	# img_warp = cv2.warpPerspective(img_np, H_np, (133, 100), flags=cv2.INTER_LINEAR)

	# if isinstance(img, torch.autograd.variable.Variable):
	# p = [3, 0, 7200, 0, 3, 7200, 0, 0]
	# p = torch.from_numpy(np.expand_dims(np.array(p,),(0,2))).to(torch.float32)
	if isinstance(img, torch.autograd.Variable):
		if USE_CUDA:
			x = Variable(torch.arange(w).cuda())
			y = Variable(torch.arange(h).cuda())
		else:
			x = Variable(torch.arange(w))
			y = Variable(torch.arange(h))
	else:
		x = torch.arange(w)
		y = torch.arange(h)

	# meshgrid()函数
	X, Y = meshgrid(x, y)

	H = param_to_H(p)

	# if isinstance(img, torch.autograd.variable.Variable):
	if isinstance(img, torch.autograd.Variable):
		if USE_CUDA:
			# create xy matrix, 2 x N
			xy = torch.cat((X.view(1, X.numel()), Y.view(1, Y.numel()), Variable(torch.ones(1, X.numel()).cuda())), 0)
		else:
			xy = torch.cat((X.view(1, X.numel()).float(), Y.view(1, Y.numel()).float(), Variable(torch.ones(1, X.numel()))), 0)
	else:
		# xy = torch.cat((X.view(1, X.numel()), Y.view(1, Y.numel()), torch.ones(1, X.numel())), 0)
		xy = torch.cat((X.view(1, X.numel()).float(), Y.view(1, Y.numel()).float(), torch.ones(1, X.numel())), 0)

	xy = xy.repeat(batch_size, 1, 1)

	# p参数融入的结果在这里
	# 这一步是warping操作的核心
	# 不过这里应该需要cat起来
	ts_1 = time.time()
	xy_warp = H.bmm(xy)
	# print("真正提取子图所用的H：", H)
	ts_2 = time.time()
	# print("测试耗时——bmm：", ts_2 - ts_1)  # 经过测试这里的耗时占据这extrac_template之中的70%

	ts_1 = time.time()
	# extract warped X and Y, normalizing the homog coordinates
	X_warp = xy_warp[:, 0, :] / xy_warp[:, 2, :]
	Y_warp = xy_warp[:, 1, :] / xy_warp[:, 2, :]

	# 这么应该也没区别
	# X_warp = xy_warp[:, 0, :]
	# Y_warp = xy_warp[:, 1, :]

	X_warp = X_warp.reshape(batch_size, h, w) + (w - 1) / 2
	Y_warp = Y_warp.reshape(batch_size, h, w) + (h - 1) / 2
	ts_2 = time.time()
	# print("测试耗时——view：", ts_2 - ts_1)  # 经过测试这里的耗时占据这extrac_template之中的70%

	# 这个函数比较重要
	ts_1 = time.time()
	img_warp, mask, xy_patch_org_cor = grid_bilinear_sampling(img, X_warp, Y_warp)
	mask_np = np.array(mask.cpu())
	# print("平均 mask", np.mean(mask_np))
	ts_2 = time.time()
	# print("测试耗时——grid_bilinear_sampling：", ts_2 - ts_1)  # 经过测试这里的耗时占据这extrac_template之中的70%
	return img_warp, mask, xy_patch_org_cor


def warp_hmg_Noncentric(img, p, xy_cor_curr, img_w = 300, img_h = 225):
	# 对于warp_hmg的自定义重写，
	# 得到warped and croped图片的坐标 xy_cor_curr，在p warp参数下，于img中的位置
	# return [x_patch_org_cor, y_patch_org_cor] 返回在大图中的位置
	# img是大地图，img_w与img_h是航拍图尺寸，两者含义不同

	img = torch.from_numpy(img).unsqueeze(0).float()
	batch_size, k, h, w = img.shape
	p = torch.from_numpy(p).float()

	# if isinstance(img, torch.autograd.variable.Variable):
	if isinstance(img, torch.autograd.Variable):
		if USE_CUDA:
			x = Variable(torch.arange(w).cuda())
			y = Variable(torch.arange(h).cuda())
		else:
			x = Variable(torch.arange(w))
			y = Variable(torch.arange(h))
	else:
		x = torch.arange(w)
		y = torch.arange(h)

	# meshgrid()函数
	X, Y = meshgrid(x, y)

	H = param_to_H(p)

	# if isinstance(img, torch.autograd.variable.Variable):
	if isinstance(img, torch.autograd.Variable):
		if USE_CUDA:
			# create xy matrix, 2 x N
			xy = torch.cat((X.view(1, X.numel()), Y.view(1, Y.numel()), Variable(torch.ones(1, X.numel()).cuda())), 0)
		else:
			xy = torch.cat((X.view(1, X.numel()), Y.view(1, Y.numel()), Variable(torch.ones(1, X.numel()))), 0)
	else:
		xy = torch.cat((X.view(1, X.numel()), Y.view(1, Y.numel()), torch.ones(1, X.numel())), 0)

	xy = xy.repeat(batch_size, 1, 1)

	# p参数融入的结果在这里
	# 这一步是warping操作的核心
	# 不过这里应该需要cat起来
	xy_warp = H.bmm(xy)
	# extract warped X and Y, normalizing the homog coordinates
	X_warp = xy_warp[:, 0, :] / xy_warp[:, 2, :]
	Y_warp = xy_warp[:, 1, :] / xy_warp[:, 2, :]

	X_warp = X_warp.view(batch_size, h, w) + (w - 1) / 2
	Y_warp = Y_warp.view(batch_size, h, w) + (h - 1) / 2

	# 我们直接使用bilinear grid sample中的函数
	if k > 3:
		x_patch_org_cor = None
		y_patch_org_cor = None
	else:
		# 在这里考虑patch的crop以及resize的问题
		# 人为指定出img的尺寸大小
		aspect = img_w / img_h
		adj_img_w = round(aspect * h)  # 计算符合长宽比的基准图宽度
		left = round(w / 2 - adj_img_w / 2)
		upper = 0
		right = round(w / 2 + adj_img_w / 2)
		lower = h
		x_cor_curr = round(xy_cor_curr[1]*(h/img_h))
		y_cor_curr = round(xy_cor_curr[0]*(h/img_h) + left + 1)
		if 0 <= x_cor_curr <= h and 0 <= y_cor_curr <= w:
			x_patch_org_cor = round(X_warp[0, x_cor_curr, y_cor_curr].item())
			y_patch_org_cor = round(Y_warp[0, x_cor_curr, y_cor_curr].item())
			# print(x_patch_org_cor, y_patch_org_cor)
		else:
			# 计算错误的情况
			x_patch_org_cor = 0
			y_patch_org_cor = 0

	return [x_patch_org_cor, y_patch_org_cor]


def grid_bilinear_sampling(A, x, y):
	# 修改
	A = A.to(device)
	# k的大小是channel的大小
	batch_size, k, h, w = A.size()
	x_norm = x / ((w - 1) / 2) - 1
	y_norm = y / ((h - 1) / 2) - 1
	grid = torch.cat((x_norm.view(batch_size, h, w, 1), y_norm.view(batch_size, h, w, 1)), 3)
	# torch.nn.functional.grid_sample 使用双线性插值
	# 以下计算得到的就是patch在原来的图中的点
	# 仅仅在通道数小于4的情况下进行计算
	if k > 3:
		x_patch_org_cor = None
		y_patch_org_cor = None
	else:
		# x_patch_org_cor = round(
		# 	(grid[0, round((h - 1) / 2), round((w - 1) / 2), 0] * ((h - 1) / 2) + (h - 1) / 2).item())
		# y_patch_org_cor = round(
		# 	(grid[0, round((h - 1) / 2), round((w - 1) / 2), 1] * ((w - 1) / 2) + (w - 1) / 2).item())
		x_patch_org_cor = round(x[0, round((h - 1) / 2), round((w - 1) / 2)].item())
		y_patch_org_cor = round(y[0, round((h - 1) / 2), round((w - 1) / 2)].item())
		# print(x_patch_org_cor, y_patch_org_cor)
	Q = grid_sample(A, grid, mode='bilinear')

	# if isinstance(A, torch.autograd.variable.Variable):
	# mask参数的计算，这里的具体意义可能是
	if isinstance(A, torch.autograd.Variable):
		if USE_CUDA:
			in_view_mask = Variable(((x_norm.data > -1 + 2 / w) & (x_norm.data < 1 - 2 / w) & (
						y_norm.data > -1 + 2 / h) & (y_norm.data < 1 - 2 / h)).type_as(A.data).cuda())
		else:
			in_view_mask = Variable(((x_norm.data > -1 + 2 / w) & (x_norm.data < 1 - 2 / w) & (
						y_norm.data > -1 + 2 / h) & (y_norm.data < 1 - 2 / h)).type_as(A.data))
	else:
		in_view_mask = ((x_norm > -1 + 2 / w) & (x_norm < 1 - 2 / w) & (y_norm > -1 + 2 / h) & (
					y_norm < 1 - 2 / h)).type_as(A)
		Q = Q.data

	return Q.view(batch_size, k, h, w), in_view_mask, [x_patch_org_cor, y_patch_org_cor]


def param_to_H(p):
	# batch parameters to batch homography
	batch_size, _, _ = p.size()

	# if isinstance(p, torch.autograd.variable.Variable):
	if isinstance(p, torch.autograd.Variable):
		if USE_CUDA:
			z = Variable(torch.zeros(batch_size, 1, 1).cuda())
		else:
			z = Variable(torch.zeros(batch_size, 1, 1))
	else:
		z = torch.zeros(batch_size, 1, 1)

	# 修改
	p = p.to(device)
	p_ = torch.cat((p, z), 1)

	# if isinstance(p, torch.autograd.variable.Variable):
	if isinstance(p, torch.autograd.Variable):
		if USE_CUDA:
			I = Variable(torch.eye(3, 3).repeat(batch_size, 1, 1).cuda())
		else:
			I = Variable(torch.eye(3, 3).repeat(batch_size, 1, 1))
	else:
		I = torch.eye(3, 3).repeat(batch_size, 1, 1)

	# 已经加了I了
	H = p_.view(batch_size, 3, 3) + I

	return H


def H_to_param(H):
	# batch homography to batch parameters
	batch_size, _, _ = H.size()

	# if isinstance(H, torch.autograd.variable.Variable):
	if isinstance(H, torch.autograd.Variable):
		if USE_CUDA:
			I = Variable(torch.eye(3, 3).repeat(batch_size, 1, 1).cuda())
		else:
			I = Variable(torch.eye(3, 3).repeat(batch_size, 1, 1))
	else:
		I = torch.eye(3, 3).repeat(batch_size, 1, 1)

	p = H - I

	p = p.view(batch_size, 9, 1)
	p = p[:, 0:8, :]

	return p


def meshgrid(x, y):
	imW = x.size(0)
	imH = y.size(0)

	x = x - torch.true_divide(x.max(), 2)
	y = y - torch.true_divide(y.max(), 2)

	X = x.unsqueeze(0).repeat(imH, 1)
	Y = y.unsqueeze(1).repeat(1, imW)
	return X, Y


class vgg16Conv(nn.Module):
	def __init__(self, model_path):
		super(vgg16Conv, self).__init__()

		print('Loading pretrained network...', end='')
		vgg16 = torch.load(model_path)
		print('done')

		self.features = nn.Sequential(
			*(list(vgg16.features.children())[0:15]),
		)

		# freeze conv1, conv2
		for p in self.parameters():
			if p.size()[0] < 256:
				p.requires_grad = False

		# Resnet没有Children
		# 直接在所有的层级上训练
		# self.features = nn.Sequential(
		# 	*(list(vgg16.children())[0:5]),
		# )
		#
		# for idx, p in enumerate(self.parameters()):
		# 	if idx == 4:
		# 		p.requires_grad = True
		# 	else:
		# 		p.requires_grad = False
		#
		# # 后面再加两层上采样层使其分辨率可达到一致。这样的方式可能会减小当前我们方法的误差
		# self.features = nn.Sequential(
		# 	*(list(vgg16.features.children())[0:15]),
		# 	nn.UpsamplingBilinear2d(scale_factor=2),
		# 	nn.UpsamplingBilinear2d(scale_factor=2)
		# )
		#
		# # freeze conv1, conv2
		# for p in self.parameters():
		# 	if p.size()[0] < 256:
		# 		p.requires_grad = False

		'''
	    (0): Conv2d (3, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (1): ReLU(inplace)
	    (2): Conv2d (64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (3): ReLU(inplace)
	    (4): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (5): Conv2d (64, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (6): ReLU(inplace)
	    (7): Conv2d (128, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (8): ReLU(inplace)
	    (9): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (10): Conv2d (128, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (11): ReLU(inplace)
	    (12): Conv2d (256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (13): ReLU(inplace)
	    (14): Conv2d (256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (15): ReLU(inplace)
	    (16): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (17): Conv2d (256, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (18): ReLU(inplace)
	    (19): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (20): ReLU(inplace)
	    (21): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (22): ReLU(inplace)
	    (23): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (24): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (25): ReLU(inplace)
	    (26): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (27): ReLU(inplace)
	    (28): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (29): ReLU(inplace)
	    (30): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    '''

	def forward(self, x):
		# print('CNN stage...',end='')
		x = self.features(x)
		# print('done')
		return x


class noPoolNet(nn.Module):
	def __init__(self, model_path):
		super(noPoolNet, self).__init__()

		print('Loading pretrained network...', end='')

		vgg16 = torch.load(model_path)

		print('done')

		vgg_features = list(vgg16.features.children())
		vgg_features[2].stride = (2, 2)
		vgg_features[7].stride = (2, 2)

		self.custom = nn.Sequential(
			*(vgg_features[0:4] +
			  vgg_features[5:9] +
			  vgg_features[10:15]),
		)

		layer = 0

		for p in self.parameters():
			if layer < 8:
				p.requires_grad = False

			layer += 1

	def forward(self, x):
		x = self.custom(x)
		return x


class vgg16fineTuneAll(nn.Module):
	def __init__(self, model_path):
		super(vgg16fineTuneAll, self).__init__()

		print('Loading pretrained network...', end='')
		vgg16 = torch.load(model_path)
		print('done')

		self.features = nn.Sequential(
			*(list(vgg16.features.children())[0:15]),
		)

		'''
	    (0): Conv2d (3, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (1): ReLU(inplace)
	    (2): Conv2d (64, 64, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (3): ReLU(inplace)
	    (4): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (5): Conv2d (64, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (6): ReLU(inplace)
	    (7): Conv2d (128, 128, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (8): ReLU(inplace)
	    (9): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (10): Conv2d (128, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (11): ReLU(inplace)
	    (12): Conv2d (256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (13): ReLU(inplace)
	    (14): Conv2d (256, 256, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (15): ReLU(inplace)
	    (16): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (17): Conv2d (256, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (18): ReLU(inplace)
	    (19): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (20): ReLU(inplace)
	    (21): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (22): ReLU(inplace)
	    (23): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    (24): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (25): ReLU(inplace)
	    (26): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (27): ReLU(inplace)
	    (28): Conv2d (512, 512, kernel_size=(3, 3), stride=(1, 1), padding=(1, 1))
	    (29): ReLU(inplace)
	    (30): MaxPool2d(kernel_size=(2, 2), stride=(2, 2), dilation=(1, 1))
	    '''

	def forward(self, x):
		x = self.features(x)
		return x


class custom_net(nn.Module):
	def __init__(self, model_path):
		super(custom_net, self).__init__()

		# print('Loading pretrained network...',end='')
		self.custom = torch.load(model_path, map_location=lambda storage, loc: storage)
		# print('done')

	def forward(self, x):
		# 直接使用Pytorch进行预测，即直接使用forward函数
		x = self.custom(x)
		# print(x.shape)
		return x

		# tensorRT transform
		# x_numpy = x.detach().numpy().reshape(-1).astype(np.float32)
		# inputs[0].host = x_numpy
		# trt_outputs = do_inference(context, bindings=bindings, inputs=inputs, outputs=outputs, stream=stream)
		# print("Inference Complete")
		# shape_of_output = (1, 256, 25, 33)
		# feat = postprocess_the_outputs(trt_outputs[0], shape_of_output)
		#
		# feat = torch.from_numpy(feat).cpu()
		# return feat


class custConv(nn.Module):
	def __init__(self, model_path):
		super(custom_net, self).__init__()

		print('Loading pretrained network...', end='')
		self.custom = torch.load(model_path)
		print('done')

	def forward(self, x):
		x = self.custom(x)
		return x




class DeepLK(nn.Module):
	def __init__(self, conv_net):
		# 这部分做初始化用
		super(DeepLK, self).__init__()
		self.img_gradient_func = GradientBatch()
		self.conv_func = conv_net
		self.inv_func = InverseBatch()

	def forward(self, img, temp, init_param=None, tol=1e-3, max_itr=500, conv_flag=0, ret_itr=False):
		# 这个迭代的速度并不快的，比SP的计算速度还要慢一点
		if conv_flag:
			start = time.time()
			Ft = self.conv_func(temp)
			stop = time.time()
			Fi = self.conv_func(img)
			# print('Feature size: '+str(Ft.size()))

		else:
			Fi = img
			Ft = temp

		batch_size, k, h, w = Ft.size()

		Ftgrad_x, Ftgrad_y = self.img_gradient_func(Ft)

		dIdp = self.compute_dIdp(Ftgrad_x, Ftgrad_y)
		dIdp_t = dIdp.transpose(1, 2)

		# 修改，参考：https://blog.csdn.net/qq_36926037/article/details/108419899
		# Pytorch 1.3之后的版本只能使用静态的forward，我理解是并不能调用另一个类进行前向传播
		# 只能使用其他的修改方法
		# invH = self.inv_func(dIdp_t.bmm(dIdp))
		invH = self.inv_func.forward(dIdp_t.bmm(dIdp))

		invH_dIdp = invH.bmm(dIdp_t)

		if USE_CUDA:
			if init_param is None:
				p = Variable(torch.zeros(batch_size, 8, 1).cuda())
			else:
				p = init_param
				# 修改
				p = p.to(device)

			# ones so that the norm of each dp is larger than tol for first iteration
			dp = Variable(torch.ones(batch_size, 8, 1).cuda())
		else:
			if init_param is None:
				p = Variable(torch.zeros(batch_size, 8, 1))
			else:
				p = init_param

			dp = Variable(torch.ones(batch_size, 8, 1))

		itr = 1

		r_sq_dist_old = 0

		while (float(dp.norm(p=2, dim=1, keepdim=True).max()) > tol or itr == 1) and (itr <= max_itr):
			# 这里会运行一次warp_hmg函数
			ts_1 = time.time()
			Fi_warp, mask, _ = warp_hmg(Fi, p)
			ts_2 = time.time()
			# print("测试耗时：",ts_2 - ts_1)	# 经过测试这里的耗时不大
			mask.unsqueeze_(1)

			mask = mask.repeat(1, k, 1, 1)

			# 修改
			# 由于这里的temp以及很多其他的变量均没有进行变量类型的判断，因此出现了很多的错误
			Ft = Ft.to(device)
			mask = mask.to(device)
			Ft_mask = Ft.mul(mask)

			r = Fi_warp - Ft_mask

			r = r.view(batch_size, k * h * w, 1)

			dp_new = invH_dIdp.bmm(r)
			dp_new[:, 6:8, 0] = 0

			if USE_CUDA:
				dp = (dp.norm(p=2, dim=1, keepdim=True) > tol).type(torch.FloatTensor).cuda() * dp_new
			else:
				dp = (dp.norm(p=2, dim=1, keepdim=True) > tol).type(torch.FloatTensor) * dp_new

			p = p - dp

			itr = itr + 1

		# show the iteration number for lucas-kanade method
		# print('finished at iteration ', itr)

		if (ret_itr):
			return p, param_to_H(p), itr
		else:
			return p, param_to_H(p)

	def compute_dIdp(self, Ftgrad_x, Ftgrad_y):

		batch_size, k, h, w = Ftgrad_x.size()

		# 修改
		Ftgrad_x = Ftgrad_x.to(device)
		Ftgrad_y = Ftgrad_y.to(device)
		# 修改，彻底的适配于硬件型号
		if USE_CUDA:
			x = torch.arange(w).cuda()
			y = torch.arange(h).cuda()
		else:
			x = torch.arange(w)
			y = torch.arange(h)

		X, Y = meshgrid(x, y)

		X = X.view(X.numel(), 1)
		Y = Y.view(Y.numel(), 1)

		X = X.repeat(batch_size, k, 1)
		Y = Y.repeat(batch_size, k, 1)

		if USE_CUDA:
			X = Variable(X.cuda())
			Y = Variable(Y.cuda())
		else:
			X = Variable(X)
			Y = Variable(Y)

		Ftgrad_x = Ftgrad_x.view(batch_size, k * h * w, 1)
		Ftgrad_y = Ftgrad_y.view(batch_size, k * h * w, 1)

		dIdp = torch.cat((
			X.mul(Ftgrad_x),
			Y.mul(Ftgrad_x),
			Ftgrad_x,
			X.mul(Ftgrad_y),
			Y.mul(Ftgrad_y),
			Ftgrad_y,
			-X.mul(X).mul(Ftgrad_x) - X.mul(Y).mul(Ftgrad_y),
			-X.mul(Y).mul(Ftgrad_x) - Y.mul(Y).mul(Ftgrad_y)), 2)

		# dIdp size = batch_size x k*h*w x 8
		return dIdp


def main():
	sz = 200
	xy = [0, 0]
	sm_factor = 8

	sz_sm = int(sz / sm_factor)

	# conv_flag = int(argv[3])

	preprocess = transforms.Compose([
		transforms.ToTensor(),
	])

	img1 = Image.open(argv[1]).crop((xy[0], xy[1], xy[0] + sz, xy[1] + sz))
	# 图片的输入以及裁剪
	img1_coarse = Variable(preprocess(img1.resize((sz_sm, sz_sm))))
	img1 = Variable(preprocess(img1))

	img2 = Image.open(argv[2]).crop((xy[0], xy[1], xy[0] + sz, xy[1] + sz))
	img2_coarse = Variable(preprocess(img2.resize((sz_sm, sz_sm))))
	img2 = Variable(preprocess(img2))  # *Variable(0.2*torch.rand(3,200,200)-1)

	transforms.ToPILImage()(img1.data).show()
	# transforms.ToPILImage()(img2.data).show()

	scale = 1.6
	angle = 15
	projective_x = 0
	projective_y = 0
	translation_x = 0
	translation_y = 0

	rad_ang = angle / 180 * pi

	p = Variable(torch.Tensor([scale + cos(rad_ang) - 2,
	                           -sin(rad_ang),
	                           translation_x,
	                           sin(rad_ang),
	                           scale + cos(rad_ang) - 2,
	                           translation_y,
	                           projective_x,
	                           projective_y]))
	p = p.view(8, 1)
	pt = p.repeat(5, 1, 1)

	# p = Variable(torch.Tensor([0.4, 0, 0, 0, 0, 0, 0, 0]))
	# p = p.view(8,1)
	# pt = torch.cat((p.repeat(10,1,1), pt), 0)

	# print(p)

	dlk = DeepLK()

	img1 = img1.repeat(5, 1, 1, 1)
	img2 = img2.repeat(5, 1, 1, 1)
	img1_coarse = img1_coarse.repeat(5, 1, 1, 1)
	img2_coarse = img2_coarse.repeat(5, 1, 1, 1)

	wimg2, _, _ = warp_hmg(img2, H_to_param(dlk.inv_func(param_to_H(pt))))

	wimg2_coarse, _, _ = warp_hmg(img2_coarse, H_to_param(dlk.inv_func(param_to_H(pt))))

	transforms.ToPILImage()(wimg2[0, :, :, :].data).show()

	img1_n = normalize_img_batch(img1)
	wimg2_n = normalize_img_batch(wimg2)

	img1_coarse_n = normalize_img_batch(img1_coarse)
	wimg2_coarse_n = normalize_img_batch(wimg2_coarse)

	start = time.time()
	print('start conv...')
	p_lk_conv, H_conv = dlk(wimg2_n, img1_n, tol=1e-4, max_itr=200, conv_flag=1)
	print('conv time: ', time.time() - start)

	start = time.time()
	print('start raw...')
	p_lk, H = dlk(wimg2_coarse_n, img1_coarse_n, tol=1e-4, max_itr=200, conv_flag=0)
	print('raw time: ', time.time() - start)

	print((p_lk_conv[0, :, :] - pt[0, :, :]).norm())
	print((p_lk[0, :, :] - pt[0, :, :]).norm())
	print(H_conv)
	print(H)

	warped_back_conv, _, _ = warp_hmg(wimg2, p_lk_conv)
	warped_back_lk, _, _ = warp_hmg(wimg2, p_lk)

	transforms.ToPILImage()(warped_back_conv[0, :, :, :].data).show()
	transforms.ToPILImage()(warped_back_lk[0, :, :, :].data).show()

	conv_loss = train.corner_loss(p_lk_conv, pt)
	lk_loss = train.corner_loss(p_lk, pt)

	pdb.set_trace()


if __name__ == "__main__":
	main()