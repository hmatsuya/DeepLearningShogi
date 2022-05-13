CC = g++
MATE_SEARCH_DEPTH = 5
# PV_MATE_SEARCH;ONNXRUNTIME;HAVE_SSE4;HAVE_SSE42;HAVE_BMI2;HAVE_AVX2;NDEBUG;_CONSOLE;
CFLAGS = -std=c++17 -Wextra -Ofast -MMD -MP -fopenmp -DPV_MATE_SEARCH -DWIN_TYPE_DOUBLE -DMATE_SEARCH_DEPTH=$(MATE_SEARCH_DEPTH) -DNDEBUG -DHAVE_SSE4 -DHAVE_SSE42 -DHAVE_BMI2 -msse4.2 -mbmi2 -DHAVE_AVX2 -mavx2 -DONNXRUNTIME
LDFLAGS = -lpthread -lnvinfer -lnvparsers -lnvonnxparser -lcudart -flto
INCLUDE = -I../usi_onnxruntime -I../usi -I../cppshogi -I../cppshogi -I/usr/local/cuda/include -I /usr/local/onnxruntime-linux-x64-1.10.0/include
LIB = -L/usr/local/cuda/lib64 -L/usr/local/onnxruntime-linux-x64-1.10.0/lib

target = bin/usi_onnxruntime
sources = nn_onnxruntime.cpp
usi_sources = main.cpp dfpn.cpp Message.cpp UctSearch.cpp Node.cpp PvMateSearch.cpp
cppshogi_sources = bitboard.cpp book.cpp common.cpp cppshogi.cpp generateMoves.cpp hand.cpp init.cpp move.cpp mt64bit.cpp position.cpp search.cpp square.cpp usi.cpp dtype.cpp
cuda_sources = unpack.cu
objects = $(addprefix obj/, $(sources:.cpp=.o))
usi_objects = $(addprefix obj/, $(usi_sources:.cpp=.o))
cppshogi_objects = $(addprefix obj/, $(cppshogi_sources:.cpp=.o))
cuda_objects = $(addprefix obj/, $(cuda_sources:.cu=.o))

$(target): $(objects) $(usi_objects) $(cppshogi_objects) $(cuda_objects)
	@[ -d bin ] || mkdir -p bin
	$(CC) -o $@ $^ $(LIB) $(LDFLAGS) $(CFLAGS)

obj/%.o: %.cpp
	@[ -d obj ] || mkdir -p obj
	$(CC) $(CFLAGS) $(INCLUDE) -o $@ -c $<

obj/%.o: ../usi/%.cpp
	@[ -d obj ] || mkdir -p obj
	$(CC) $(CFLAGS) $(INCLUDE) -o $@ -c $<

obj/%.o: ../cppshogi/%.cpp
	@[ -d obj ] || mkdir -p obj
	$(CC) $(CFLAGS) $(INCLUDE) -o $@ -c $<

obj/%.o: ../usi/%.cu
	nvcc -x cu -I../cppshogi -maxrregcount=0 --machine 64 --compile -cudart static -DNDEBUG -Xcompiler "-Ofast" -o $@ $<

all: $(target)

clean:
	rm -f $(objects) $(cppshogi_objects) $(target) $(cuda_objects) $(usi_objects)